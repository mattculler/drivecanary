"""Push, host side: the real agent script, run as its timer would, against a real listener."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from drivecanary import push
from drivecanary.config import Config
from drivecanary.ingest_api import create_ingest_app
from drivecanary.models import AttrlogCursor, Host, HostAttempt, HostState, SmartRun, Transport
from tests.conftest import ProbeEnv


def _push_host(factory: sessionmaker[Session], name: str = "pve") -> str:
    token, hashed = push.new_token()
    with factory() as s:
        s.add(Host(name=name, address=name, transport=Transport.PUSH.value, push_token_hash=hashed))
        s.commit()
    return token


def _host(factory: sessionmaker[Session], name: str = "pve") -> Host:
    with factory() as s:
        host = s.scalar(select(Host).where(Host.name == name))
        assert host is not None
        s.expunge(host)
        return host


@pytest.fixture
def hub(cfg: Config, factory: sessionmaker[Session]) -> Iterator[str]:
    """The ingest listener on a free local port, in a thread."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_ingest_app(cfg), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _spool(env: ProbeEnv) -> list[str]:
    return sorted(p.name for p in (env.agent_state / "spool").glob("*.env.gz"))


def test_agent_reports_and_remembers_where_the_hub_is(
    cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv, hub: str
) -> None:
    token = _push_host(factory)
    cp = probe_env.run_agent(hub, token)
    assert cp.returncode == 0, cp.stderr
    assert "agent: delivered" in cp.stdout and "runs=3" in cp.stdout
    assert _spool(probe_env) == []
    offsets = (probe_env.agent_state / "offsets").read_text()
    with factory() as s:
        cursor = s.scalar(select(AttrlogCursor))
        assert cursor is not None and offsets == f"{cursor.file_name}={cursor.offset}\n"
        assert s.scalar(select(func.count(SmartRun.id)).where(SmartRun.source == "attrlog")) == 20
    assert _host(factory).state == HostState.OK.value

    with (probe_env.attrlog_dir / cursor.file_name).open("a") as fh:  # smartd writes another line
        fh.write("2023-12-18 07:45:25;\t5;100;0;\t9;100;272;\t194;33;98784247841;\n")
    time.sleep(1.1)  # spool names are whole seconds
    cp = probe_env.run_agent(hub, token)
    assert cp.returncode == 0 and "attrlog_lines=1" in cp.stdout, cp.stdout + cp.stderr
    with factory() as s:
        assert s.scalar(select(func.count(SmartRun.id)).where(SmartRun.source == "attrlog")) == 21


def test_agent_spools_while_the_hub_is_away_and_says_so_after(
    cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv, hub: str
) -> None:
    token = _push_host(factory)
    cp = probe_env.run_agent("http://127.0.0.1:9", token)  # nothing listens there
    assert cp.returncode == 1 and "could not reach" in cp.stderr
    assert len(_spool(probe_env)) == 1
    time.sleep(1.1)
    cp = probe_env.run_agent("http://127.0.0.1:9", token)
    assert cp.returncode == 1 and len(_spool(probe_env)) == 2
    with factory() as s:
        assert s.scalar(select(func.count(HostAttempt.id))) == 0

    time.sleep(1.1)
    cp = probe_env.run_agent(hub, token)  # the hub is back
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.count("agent: delivered") == 3 and _spool(probe_env) == []
    assert (probe_env.agent_state / "failures").read_text() == ""
    with factory() as s:
        attempts = list(s.scalars(select(HostAttempt).order_by(HostAttempt.id)))
        assert len(attempts) == 3 and all(a.ok for a in attempts)
        assert "agent: 2 delivery failure(s) before this one" in (attempts[0].reason or "")
        assert "could not reach" in (attempts[0].reason or "")
        assert attempts[1].reason is None or "delivery failure" not in attempts[1].reason
        # the shimmed smartctl answers with the same clock reading every time, so the second and third
        # envelopes' readings are the first's again and are not stored twice; a real host's would be
        assert s.scalar(select(func.count(SmartRun.id)).where(SmartRun.source == "smartctl")) == 3


def test_agent_keeps_its_spool_when_its_token_is_refused(
    cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv, hub: str
) -> None:
    _push_host(factory)
    cp = probe_env.run_agent(hub, "some-other-token")
    assert cp.returncode == 1 and "refused this host's token (403)" in cp.stderr
    assert len(_spool(probe_env)) == 1, "a wrong token is fixed on the host; what was collected waits"


def test_agent_bounds_its_spool(cfg: Config, probe_env: ProbeEnv) -> None:
    text = probe_env.agent.read_text().replace("SPOOL_MAX_FILES=24", "SPOOL_MAX_FILES=2")
    probe_env.agent.write_text(text)
    for _ in range(4):
        probe_env.run_agent("http://127.0.0.1:9", "t")
        time.sleep(1.1)
    assert len(_spool(probe_env)) == 2
    assert "spool full: dropped" in (probe_env.agent_state / "failures").read_text()

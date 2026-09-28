from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from drivecanary.collect import SshResult, classify, collect, ssh_config_text
from drivecanary.config import Config
from drivecanary.models import CollectionRun, FailureClass, Host, HostAttempt, HostState
from tests.conftest import ProbeEnv


def _host(factory: sessionmaker[Session], name: str = "atlas") -> int:
    with factory() as s:
        h = Host(name=name, address=f"{name}.domain", tz="America/New_York", hostkey=f"{name}.domain ssh-ed25519 AAAA")
        s.add(h)
        s.commit()
        return h.id


def test_ssh_config_pins_everything(cfg: Config) -> None:
    text = ssh_config_text(cfg, [Host(name="atlas", address="atlas.domain", ssh_port=2222, ssh_user=None)])
    assert "Host atlas\n    HostName atlas.domain\n    Port 2222\n    User drivecanary" in text
    for must in (
        "IdentitiesOnly yes",
        "StrictHostKeyChecking yes",
        "BatchMode yes",
        "ConnectTimeout 10",
        "known_hosts",
    ):
        assert must in text


def test_classify() -> None:
    assert (
        classify(SshResult(255, b"", b"ssh: connect to host x port 22: Connection timed out"), None, None)[0]
        == FailureClass.UNREACHABLE
    )
    assert classify(SshResult(255, b"", b"x: Permission denied (publickey)."), None, None)[0] == FailureClass.AUTH
    assert classify(SshResult(255, b"", b"Host key verification failed."), None, None)[0] == FailureClass.HOSTKEY
    assert (
        classify(SshResult(127, b"", b"bash: drivecanary-collect: command not found"), None, None)[0]
        == FailureClass.KEY_NOT_RESTRICTED
    )
    assert classify(SshResult(-1, b"", b"", timed_out=True), None, None)[0] == FailureClass.TIMEOUT
    assert classify(SshResult(64, b"", b"drivecanary gate: refused"), None, None)[0] == FailureClass.ENVELOPE


def test_collect_success_and_failure(cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv) -> None:
    _host(factory, "atlas")
    _host(factory, "storage1")
    envelope = probe_env.run_gate("drivecanary-collect").stdout

    def runner(host: Host, args: list[str]) -> SshResult:
        if host.name == "atlas":
            assert args == []
            return SshResult(0, envelope, b"")
        return SshResult(255, b"", b"ssh: connect to host storage1.domain port 22: No route to host\n")

    run, outcomes = collect(cfg, factory, runner=runner, trigger="manual")
    assert run.hosts_expected == 2 and run.hosts_ok == 1 and run.hosts_failed == 1
    by = {o.name: o for o in outcomes}
    assert by["atlas"].ok and by["atlas"].result is not None and by["atlas"].result.runs == 3
    assert not by["storage1"].ok and by["storage1"].failure_class == FailureClass.UNREACHABLE
    with factory() as s:
        atlas = s.scalar(select(Host).where(Host.name == "atlas"))
        vault = s.scalar(select(Host).where(Host.name == "storage1"))
        assert atlas is not None and atlas.state == HostState.OK.value and atlas.last_success_at is not None
        assert vault is not None and vault.state == HostState.UNREACHABLE.value and vault.unreachable_since is not None
        assert vault.last_attempt_class == "unreachable" and "No route" in (vault.last_attempt_reason or "")
        attempts = list(s.scalars(select(HostAttempt)))
        assert len(attempts) == 2 and sum(a.ok for a in attempts) == 1
        assert s.scalar(select(CollectionRun)) is not None
    # the generated ssh material exists and pins the keys
    assert (cfg.ssh_dir / "config").exists() and "atlas.domain ssh-ed25519" in (cfg.ssh_dir / "known_hosts").read_text()

    # second run: atlas's cursor offset is passed to the gate
    seen: list[list[str]] = []

    def runner2(host: Host, args: list[str]) -> SshResult:
        seen.append(args)
        return SshResult(0, probe_env.run_gate("drivecanary-collect " + " ".join(args)).stdout, b"")

    run2, _ = collect(cfg, factory, runner=runner2, only=["atlas"])
    assert run2.hosts_ok == 1 and seen and seen[0][0].startswith("attrlog.ST20000NM007D_3DJ103-ZXA00001.ata.csv=")


def test_unreachable_backoff(cfg: Config, factory: sessionmaker[Session]) -> None:
    from datetime import timedelta

    from drivecanary.timeutil import utcnow

    hid = _host(factory, "storage1")
    with factory() as s:
        h = s.get(Host, hid)
        assert h is not None
        h.state = HostState.UNREACHABLE.value
        h.unreachable_since = utcnow() - timedelta(days=3)
        h.last_attempt_at = utcnow() - timedelta(hours=1)
        s.commit()
    calls: list[str] = []

    def runner(host: Host, args: list[str]) -> SshResult:
        calls.append(host.name)
        return SshResult(255, b"", b"No route to host")

    run, _ = collect(cfg, factory, runner=runner)
    assert run.hosts_expected == 0 and calls == [], "down for days and tried an hour ago: not due yet"
    run, _ = collect(cfg, factory, runner=runner, only=["storage1"])
    assert run.hosts_expected == 1 and calls == ["storage1"], "named explicitly: always tried"

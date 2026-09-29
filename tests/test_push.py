"""Push, hub side: what the receiver stores and what it refuses."""

from __future__ import annotations

import gzip

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from drivecanary import push
from drivecanary.config import Config
from drivecanary.ingest_api import create_ingest_app
from drivecanary.models import Host, HostAttempt, HostState, SmartRun, Transport
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


def test_tokens_are_kept_as_hashes() -> None:
    token, hashed = push.new_token()
    assert len(token) >= 40 and hashed == push.hash_token(token) and token not in hashed and len(hashed) == 64
    assert push.new_token()[0] != token


def test_receive_stores_what_a_pull_would_have(
    cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv
) -> None:
    token = _push_host(factory)
    envelope = probe_env.run_gate("drivecanary-collect").stdout
    reply = push.receive(cfg, factory, token=token, body=gzip.compress(envelope), encoding="gzip")
    assert reply.status == 200, reply.text
    assert reply.lines[0].startswith("ok host=pve payload_id=") and "runs=3 pools=3 attrlog_lines=20" in reply.lines[0]
    assert reply.lines[-1].startswith("cursor attrlog.ST20000NM007D_3DJ103-ZXA00001.ata.csv=")
    host = _host(factory)
    assert host.state == HostState.OK.value and host.last_success_at is not None and host.machine_id
    with factory() as s:
        attempt = s.scalar(select(HostAttempt))
        assert attempt is not None and attempt.ok and attempt.transport == "push" and attempt.payload_id
        assert s.scalar(select(func.count(SmartRun.id)).where(SmartRun.source == "smartctl")) == 3

    again = push.receive(cfg, factory, token=token, body=envelope)  # a retry, uncompressed this time
    assert again.status == 200 and "duplicate=1" in again.lines[0] and again.lines[-1] == reply.lines[-1]
    with factory() as s:
        assert s.scalar(select(func.count(HostAttempt.id))) == 1, "a retry of a stored payload stores nothing"


def test_receive_refuses(cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv) -> None:
    token = _push_host(factory)
    envelope = probe_env.run_gate("drivecanary-collect").stdout
    assert push.receive(cfg, factory, token=None, body=envelope).status == 401
    assert push.receive(cfg, factory, token="not-the-token", body=envelope).status == 403
    assert push.receive(cfg, factory, token=push.hash_token(token), body=envelope).status == 403, "the hash is no token"
    with factory() as s:
        s.add(Host(name="puller", address="puller", push_token_hash=push.hash_token("pulltoken")))
        s.commit()
    assert push.receive(cfg, factory, token="pulltoken", body=envelope).status == 403, "a pull host has no token"
    with factory() as s:
        assert s.scalar(select(func.count(HostAttempt.id))) == 0, "nothing is recorded for a stranger"

    bad = push.receive(cfg, factory, token=token, body=b"uid=0(root) gid=0(root)\n")
    assert bad.status == 400 and bad.lines[0].startswith("failed class=envelope")
    assert _host(factory).state == HostState.BROKEN.value and _host(factory).last_attempt_class == "envelope"
    assert push.receive(cfg, factory, token=token, body=b"\x1f\x8bnot gzip", encoding="gzip").status == 400

    cfg.collect.max_output_mb = 1
    bomb = gzip.compress(b"DRIVECANARY-ENVELOPE 1\n" + b"\0" * (8 * 1024 * 1024))
    assert len(bomb) < 64 * 1024
    assert push.receive(cfg, factory, token=token, body=bomb, encoding="gzip").status == 413
    cfg.collect.max_output_mb = 64

    with factory() as s:
        host = s.scalar(select(Host).where(Host.name == "pve"))
        assert host is not None
        host.state = HostState.PAUSED.value
        s.commit()
    paused = push.receive(cfg, factory, token=token, body=envelope)
    assert paused.status == 403 and "paused" in paused.text


def test_a_probe_that_could_not_run_is_delivered_but_failed(
    cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv
) -> None:
    token = _push_host(factory)
    (probe_env.shims / "sudo").write_text("#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n")
    envelope = probe_env.run_gate("drivecanary-collect").stdout
    reply = push.receive(cfg, factory, token=token, body=envelope)
    assert reply.status == 200 and reply.lines[0].startswith("failed class=sudo"), (
        "nothing to retry: the host must be fixed"
    )
    assert _host(factory).state == HostState.BROKEN.value


def test_listener_refuses_a_body_over_the_cap(cfg: Config, factory: sessionmaker[Session]) -> None:
    from fastapi.testclient import TestClient

    cfg.ingest.max_body_mb = 1
    token = _push_host(factory)
    client = TestClient(create_ingest_app(cfg))
    r = client.post("/api/ingest", content=b"x" * (2 * 1024 * 1024), headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 413
    r = client.post("/api/ingest", content=b"x")
    assert r.status_code == 401
    assert client.get("/healthz").json()["service"] == "ingest"

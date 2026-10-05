"""The JSON API: what the page says about a host, for another service to ask."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from drivecanary.config import Config
from drivecanary.models import Host
from drivecanary.timeutil import utcnow
from drivecanary.web.app import create_app
from tests.conftest import ProbeEnv
from tests.test_web import _populated


def test_one_host(cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv) -> None:
    cfg.collect.stale_after_hours = 1e9  # the fixtures' readings are from years ago
    _populated(cfg, factory, probe_env)
    r = TestClient(create_app(cfg)).get("/api/v1/host/atlas")
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"
    h = r.json()
    assert h["host"] == "atlas" and h["ok"] is False and h["status"] == "fail"
    assert h["state"] == "ok" and h["transport"] == "pull" and h["last_success_at"].endswith("Z")
    assert h["url"] == "/host/atlas" and h["api"] == "/api/v1/host/atlas"
    assert len(h["problems"]) == 3, h["problems"]
    assert any(
        p.startswith("/dev/sdb Hitachi HDS721050DLE630 (MSK423Y20S3HBC): fail: SMART overall health")
        for p in h["problems"]
    )
    assert any(p.startswith("zfs pool backup: fail") for p in h["problems"])
    assert any(p.startswith("btrfs pool /mnt/btr: warn") for p in h["problems"])
    drives = {d["dev"]: d for d in h["drives"]}
    assert set(drives) == {"/dev/sda", "/dev/sdb", "/dev/nvme0"} and h["drive_count"] == 3
    wd = drives["/dev/sda"]
    assert wd["verdict"] == "ok" and wd["reasons"] == [] and wd["model"] == "WDC WD140EDFZ-11A0VA0"
    assert wd["temperature_c"] == 32 and wd["power_on_hours"] == 1730 and wd["read_at"].endswith("Z")
    assert wd["kind"] == "hdd" and wd["capacity_bytes"] > 0 and wd["url"].startswith("/drive/")
    assert wd["self_test_percent"] == 90, "the fixture's WD is in a self-test"
    pools = {p["name"]: p for p in h["pools"]}
    assert pools["backup"]["health"] == "DEGRADED" and pools["backup"]["verdict"] == "fail"
    assert pools["tank"]["verdict"] == "ok" and pools["tank"]["reasons"] == []


def test_stale_is_a_problem(cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv) -> None:
    _populated(cfg, factory, probe_env)  # readings from years ago, judged at the default three hours
    h = TestClient(create_app(cfg)).get("/api/v1/host/atlas").json()
    nvme = next(d for d in h["drives"] if d["dev"] == "/dev/nvme0")
    assert nvme["verdict"] == "stale"
    assert any(
        p.startswith("/dev/nvme0 INTEL SSDPEKNW010T8 (BTNH93710FS91P0B): stale: last read") for p in h["problems"]
    )


def test_a_host_with_nothing_wrong_is_ok(cfg: Config, factory: sessionmaker[Session]) -> None:
    with factory() as s:
        s.add(Host(name="quiet", address="quiet", state="ok", last_success_at=utcnow() - timedelta(minutes=5)))
        s.add(Host(name="gone", address="gone", state="retired"))
        s.add(Host(name="new", address="new"))
        s.commit()
    client = TestClient(create_app(cfg))
    h = client.get("/api/v1/host/quiet").json()
    assert h["ok"] is True and h["status"] == "ok" and h["problems"] == [] and h["drives"] == [] and h["pools"] == []
    new = client.get("/api/v1/host/new").json()
    assert new["ok"] is False and new["problems"] == ["host never collected from yet"] and new["status"] == "unknown"
    gone = client.get("/api/v1/host/gone").json()
    assert gone["status"] == "retired" and gone["problems"] == ["host retired: not collected from"]
    listing = client.get("/api/v1/hosts").json()
    assert [h["host"] for h in listing["hosts"]] == ["new", "quiet"], "retired hosts are left out, as on the page"
    assert listing["ok"] is False and "drives" not in listing["hosts"][0], "the list is the summary"
    nobody = client.get("/api/v1/host/nobody")
    assert nobody.status_code == 404 and nobody.json() == {"error": "no such host: nobody"}


def test_an_unreachable_host_says_why(cfg: Config, factory: sessionmaker[Session]) -> None:
    since = utcnow() - timedelta(hours=5)
    with factory() as s:
        s.add(
            Host(
                name="hv",
                address="hv",
                state="unreachable",
                unreachable_since=since,
                last_attempt_class="unreachable",
                last_attempt_reason="ssh: connect to host hv port 22: No route to host",
            )
        )
        s.commit()
    h = TestClient(create_app(cfg)).get("/api/v1/host/hv").json()
    assert h["ok"] is False and h["status"] == "error"
    assert h["problems"][0].startswith("host unreachable since 20") and "No route to host" in h["problems"][0]

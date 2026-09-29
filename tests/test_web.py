from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from drivecanary.collect import SshResult, collect, write_ssh_material
from drivecanary.config import Config
from drivecanary.models import Host, Pool
from drivecanary.web.app import create_app, fmt_ago, fmt_bytes
from tests.conftest import ProbeEnv


def _populated(cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv) -> None:
    with factory() as s:
        s.add(Host(name="atlas", address="atlas.domain", tz="America/New_York"))
        s.commit()
    envelope = probe_env.run_gate("drivecanary-collect").stdout
    collect(cfg, factory, runner=lambda h, a: SshResult(0, envelope, b""))


def test_pages(cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv) -> None:
    _populated(cfg, factory, probe_env)
    client = TestClient(create_app(cfg))
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Hitachi HDS721050DLE630" in body and "fail" in body and "atlas" in body and "tank" in body
    assert body.index("Hitachi") < body.index("WDC WD140EDFZ"), "worst first"
    assert "finished 2026-09-14 08:12, took 5:12:33; no errors found" in body, "the scrub cell says what matters"
    assert "Rate:" not in body
    r = client.get("/hosts")
    assert r.status_code == 200 and "atlas" in r.text
    assert "<details" in r.text and "Adding a host" in r.text and "install-host.sh HOST --hub-ip" in r.text
    assert "install-host.sh HOST --push --hub-url http://THIS-VM-IP:8081 --token TOKEN" in r.text
    assert "sudo cat" in r.text, "no hub key copy yet: the page says where to get it"
    (cfg.ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAAtestkey drivecanary-hub@test\n")
    write_ssh_material(cfg, [])
    r = TestClient(create_app(cfg), base_url="http://10.100.100.50:8080").get("/hosts")
    assert "--hub-ip 10.100.100.50" in r.text and "ssh-ed25519 AAAAtestkey drivecanary-hub@test" in r.text
    assert "sudo cat" not in r.text
    r = TestClient(create_app(cfg), base_url="http://127.0.0.1:8080").get("/hosts")
    assert "--hub-ip THIS-VM-IP" in r.text, "loopback is never the address a host should accept the key from"
    r = client.get("/host/1")
    assert r.status_code == 200 and "attrlog.ST20000NM007D_3DJ103-ZXA00001.ata.csv" in r.text
    r = client.get("/runs")
    assert r.status_code == 200 and "manual" in r.text
    with factory() as s:
        btr = s.scalar(select(Pool).where(Pool.kind == "btrfs"))
        assert btr is not None
    r = client.get(f"/pool/{btr.id}")
    assert r.status_code == 200
    as_printed = "<pre>UUID:             1234-uuid\nScrub started:    Sun Sep 14 03:00:00 2026\n"
    assert as_printed in r.text, "the scrub output keeps its lines"
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] and r.json()["last_collection_age_hours"] is not None
    for icon in ("/favicon.ico", "/static/favicon-16.png", "/static/favicon-32.png", "/static/apple-touch-icon.png"):
        got = client.get(icon)
        assert got.status_code == 200 and got.content.startswith(b"\x89PNG"), icon
    assert 'href="/static/favicon-32.png"' in body and "favicon.svg" not in body
    assert client.get("/static/uPlot.iife.min.js").status_code == 200
    assert client.get("/drive/999").status_code == 404


def test_drive_page_and_series(cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv) -> None:
    _populated(cfg, factory, probe_env)
    client = TestClient(create_app(cfg))
    with factory() as s:
        from sqlalchemy import select

        from drivecanary.models import Drive

        attrlog_drive = s.scalar(select(Drive).where(Drive.serial_key == "ZXA00001"))
        nvme = s.scalar(select(Drive).where(Drive.protocol == "NVMe"))
        assert attrlog_drive is not None and nvme is not None
        ids = (attrlog_drive.id, nvme.id)
    r = client.get(f"/drive/{ids[0]}")
    assert (
        r.status_code == 200 and "ST20000NM007D" in r.text and 'data-metric="attr:5"' in r.text and "Trends" in r.text
    )
    assert "<b>30d</b>" in r.text and 'href="?window=30d"' not in r.text and 'href="?window=7d"' in r.text
    picked = client.get(f"/drive/{ids[0]}?window=1y").text
    assert "<b>1y</b>" in picked and 'href="?window=1y"' not in picked and 'href="?window=30d"' in picked
    r = client.get(f"/api/drives/{ids[0]}/series?metric=attr:194&window=all")
    assert r.status_code == 200
    pts = r.json()["points"]
    assert len(pts) == 20 and pts[0][1] == 32.0 and pts[0][0] < pts[-1][0]
    r = client.get(f"/api/drives/{ids[0]}/series?metric=temp&window=7d")
    assert r.status_code == 200 and r.json()["points"] == []  # 2023 data is outside a 7-day window
    r = client.get(f"/drive/{ids[1]}")
    assert r.status_code == 200 and 'data-metric="nvme_percentage_used"' in r.text
    assert client.get(f"/api/drives/{ids[1]}/series?metric=bogus").status_code == 400


def test_a_sata_ssd_gets_its_wear_charted() -> None:
    from drivecanary.models import Drive
    from drivecanary.web.app import metrics_for

    ssd = Drive(model_key="x", serial_key="y", protocol="ATA", rotation_rate=0)
    metrics = [m["metric"] for m in metrics_for(ssd, {5, 9, 177, 187, 241})]
    assert metrics == ["temp", "poh", "attr:177:value", "attr:5", "attr:187"]


def test_formatters() -> None:
    assert fmt_bytes(20000588955136) == "20.0 TB" and fmt_bytes(500107862016) == "500 GB" and fmt_bytes(None) == ""
    from datetime import timedelta

    from drivecanary.timeutil import utcnow

    now = utcnow()
    assert fmt_ago(now - timedelta(minutes=5), now) == "5 min ago"
    assert fmt_ago(now - timedelta(hours=3), now) == "3.0 h ago"
    assert fmt_ago(now - timedelta(days=4), now) == "4 d ago" and fmt_ago(None) == "never"

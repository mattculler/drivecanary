from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from drivecanary.collect import SshResult, collect, write_ssh_material
from drivecanary.config import Config, WebConfig
from drivecanary.models import Drive, Host, Pool
from drivecanary.timeutil import shown
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
    r = client.get("/host/atlas")
    assert r.status_code == 200 and "attrlog.ST20000NM007D_3DJ103-ZXA00001.ata.csv" in r.text
    assert client.get("/host/1").text == r.text, "a link by number from before still works"
    assert client.get("/host/nobody").status_code == 404 and client.get("/host/999").status_code == 404
    assert 'href="/host/atlas"' in body and 'href="/host/1"' not in body
    assert "<dt>smartctl</dt><dd>7.4</dd>" in r.text
    assert "<dt>drivecanary</dt><dd>gate v2, probe v3</dd>" in r.text and "behind" not in r.text
    assert "<h1>atlas <span" in r.text and "atlas.domain" in r.text
    with factory() as s:
        host = s.scalar(select(Host))
        assert host is not None
        host.probe_version, host.address = 1, "atlas"
        s.commit()
    old = client.get("/host/atlas").text
    assert "behind: probe is v3 now. Run install-host.sh for this host again." in old
    assert "<h1>atlas</h1>" in old, "an address that is the name again says nothing"
    r = client.get("/runs")
    assert r.status_code == 200 and "manual" in r.text
    with factory() as s:
        btr = s.scalar(select(Pool).where(Pool.kind == "btrfs"))
        assert btr is not None
    r = client.get(f"/pool/{btr.id}")
    assert r.status_code == 200
    as_printed = "<pre>UUID:             1234-uuid\nScrub started:    Sun Sep 14 03:00:00 2026\n"
    assert as_printed in r.text, "the scrub output keeps its lines"
    assert "not matched to a drive this host reports" in r.text, "sde reports nothing: listed, not linked"
    with factory() as s:
        tank = s.scalar(select(Pool).where(Pool.name == "tank"))
        assert tank is not None
    r = client.get(f"/pool/{tank.id}")
    members, others = r.text.split("Other disks on atlas")
    assert "WDC WD140EDFZ-11A0VA0" in members and "Hitachi HDS721050DLE630" in members and "INTEL" not in members
    assert "INTEL SSDPEKNW010T8" in others and "WDC" not in others.split("<h2>")[0]
    assert members.count('href="/drive/') == 2 and others.split("<h2>")[0].count('href="/drive/') == 1
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] and r.json()["last_collection_age_hours"] is not None
    icons = ("/favicon.ico", "/static/favicon-16.png", "/static/favicon-32.png", "/static/apple-touch-icon.png")
    for icon in (*icons, "/static/logo-tile.png"):
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
    with factory() as s:
        failing = s.scalar(select(Drive).where(Drive.model == "Hitachi HDS721050DLE630"))
        assert failing is not None
    page = client.get(f"/drive/{failing.id}").text
    assert "<dt>error log</dt><dd>56 in all; of the 5 the drive still holds:" in page
    assert "<b>5</b> the drive could not read or find a sector" in page
    assert "last self-test: Short offline, Completed without error, " in page and "power-on hours ago" in page
    assert "Reallocated_Sector_Ct" in page and "unknown to smartctl" not in page
    r = client.get(f"/drive/{ids[1]}")
    assert r.status_code == 200 and 'data-metric="nvme_percentage_used"' in r.text
    assert client.get(f"/api/drives/{ids[1]}/series?metric=bogus").status_code == 400


def test_the_summary_counts_every_host_however_it_is_monitored(
    cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv
) -> None:
    """It read "1 of 1 hosts ok" with three hosts reporting: the count came from the last collection run,
    and only pull hosts are in a run."""
    import gzip

    from drivecanary import push, queries
    from drivecanary.models import HostState, Transport

    _populated(cfg, factory, probe_env)  # atlas, pulled
    tokens = {}
    with factory() as s:
        for name in ("pve", "opnsense", "nas"):
            tokens[name], hashed = push.new_token()
            s.add(Host(name=name, address=name, transport=Transport.PUSH.value, push_token_hash=hashed))
        s.add(Host(name="attic", address="attic", state=HostState.PAUSED.value))
        s.commit()
    envelope = gzip.compress(probe_env.run_gate("drivecanary-collect").stdout)
    for name in ("pve", "opnsense"):  # nas has not reported yet
        assert push.receive(cfg, factory, token=tokens[name], body=envelope, encoding="gzip").status == 200
        envelope = gzip.compress(probe_env.run_gate("drivecanary-collect").stdout)
    with factory() as s:
        ov = queries.overview(s, cfg)
    assert (ov.hosts_ok, ov.hosts_watched) == (3, 4), "the paused one is not watched; nas is, and is not ok yet"
    assert ov.last_run is not None and ov.last_run.hosts_expected == 1
    body = TestClient(create_app(cfg)).get("/").text
    assert "3 of 4 hosts ok, the latest heard from" in body and "1 of 1 hosts" not in body


def test_a_sata_ssd_gets_its_wear_charted() -> None:
    from drivecanary.smart import parse_report
    from drivecanary.web.app import attr_names, metrics_for
    from tests.test_smart import wd_blue

    ssd = Drive(model_key="x", serial_key="y", protocol="ATA", rotation_rate=0)
    # nothing but an attribute log read: ids are all there is to go by
    metrics = [m["metric"] for m in metrics_for(ssd, {5, 9, 177, 187, 233, 241})]
    assert metrics == ["temp", "poh", "attr:177:value", "attr:5", "attr:187"]
    # smartctl has read it: its names decide, and 233 is not wear on this drive
    report = parse_report(wd_blue())
    found = metrics_for(ssd, {a.id for a in report.attrs}, report)
    assert [m["metric"] for m in found] == ["temp", "poh", "attr:230:value", "attr:5"]
    assert found[2]["label"] == "Media_Wearout_Indicator (230), normalized"
    assert attr_names(report)[173] == "Average_PE_Cycles_TLC" and 244 not in attr_names(report)
    report.endurance_used, report.ata_error_count = 4, 2
    assert [m["metric"] for m in metrics_for(ssd, {5, 230}, report)] == [
        "temp",
        "poh",
        "endurance_used",
        "attr:230:value",
        "attr:5",
        "ata_errors",
    ]


def test_times_are_shown_in_the_zone_the_page_is_set_to(cfg: Config) -> None:
    assert cfg.web.timezone == "America/New_York"
    assert shown(datetime(2026, 9, 29, 2, 1, tzinfo=UTC), "America/New_York") == "2026-09-28 22:01 EDT"
    assert shown(datetime(2026, 12, 1, 2, 1, tzinfo=UTC), "America/New_York") == "2026-11-30 21:01 EST"
    assert shown(None, "America/New_York") == ""
    with pytest.raises(ValidationError):
        WebConfig(timezone="Mars/Olympus_Mons")


def test_pages_say_est_or_edt(cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv) -> None:
    _populated(cfg, factory, probe_env)
    client = TestClient(create_app(cfg))
    for url in ("/", "/host/atlas", "/runs", "/drive/1", "/pool/1"):
        page = client.get(url).text
        assert " UTC" not in page, url
        assert url == "/pool/1" or re.search(r"\d\d:\d\d E[SD]T", page), url


def test_what_is_running_is_said_wherever_the_thing_is_named(
    cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv
) -> None:
    (probe_env.shims.parent / "btrfs.scrub").write_text(
        "UUID: 1234-uuid\nScrub started:    Mon Sep 28 02:00:01 2026\nStatus:           running\n"
        "Duration:         0:10:05\nBytes scrubbed:   1.00TiB  (18.42%)\nError summary:    no errors found\n"
    )
    cfg.collect.stale_after_hours = 1e9  # the fixtures' readings are from 2021
    _populated(cfg, factory, probe_env)
    client = TestClient(create_app(cfg))
    body = client.get("/").text
    assert "1 self-test, 1 scrub running" in body
    assert body.count("self-test 90%") == 1 and body.count(">scrub 18.42%<") == 1
    with factory() as s:
        wd = s.scalar(select(Drive).where(Drive.model == "WDC WD140EDFZ-11A0VA0"))
        tank = s.scalar(select(Pool).where(Pool.name == "tank"))
        btr = s.scalar(select(Pool).where(Pool.kind == "btrfs"))
        assert wd is not None and tank is not None and btr is not None
    assert "self-test 90%" in client.get(f"/drive/{wd.id}").text.split("</h1>")[0]
    assert (
        "self-test 90%" in client.get(f"/pool/{tank.id}").text
        and "scrub" not in client.get(f"/pool/{tank.id}").text.split("</h1>")[0]
    )
    assert "scrub 18.42%" in client.get(f"/pool/{btr.id}").text.split("</h1>")[0]
    host = client.get("/host/atlas").text
    assert "self-test 90%" in host and "scrub 18.42%" in host


def test_a_hosts_page_says_when_its_drives_test_themselves(
    cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv
) -> None:
    _populated(cfg, factory, probe_env)
    client = TestClient(create_app(cfg))
    page = client.get("/host/atlas").text
    section = page.split("<h2>Self-tests")[1].split("<h2>Attempts")[0]
    assert "smartd is active" in section
    assert (
        "<code>(S/../17/./01|L/01/19/./01)</code>" in section and "<code>(S/../17/./02|L/07/19/./01)</code>" in section
    )
    assert "-17 01:00" in section and "-01-19 01:00" in section and "-07-19 01:00" in section
    assert section.count("none scheduled") == 1, "the NVMe drive is left to DEVICESCAN, which schedules nothing"
    assert "24.6 h" in section, "what the 14 TB drive says its long test takes"
    assert "Short offline: Completed without error" in section
    with factory() as s:
        host = s.scalar(select(Host))
        assert host is not None
        host.smartd_state = "inactive"
        s.commit()
    assert "nothing running to start the tests: smartd is inactive" in client.get("/host/atlas").text
    with factory() as s:
        host = s.scalar(select(Host))
        assert host is not None
        host.smartd_conf = None
        s.commit()
    assert "has not said what its schedule is" in client.get("/host/atlas").text


def test_formatters() -> None:
    assert fmt_bytes(20000588955136) == "20.0 TB" and fmt_bytes(500107862016) == "500 GB" and fmt_bytes(None) == ""
    from datetime import timedelta

    from drivecanary.timeutil import utcnow

    now = utcnow()
    assert fmt_ago(now - timedelta(minutes=5), now) == "5 min ago"
    assert fmt_ago(now - timedelta(hours=3), now) == "3.0 h ago"
    assert fmt_ago(now - timedelta(days=4), now) == "4 d ago" and fmt_ago(None) == "never"

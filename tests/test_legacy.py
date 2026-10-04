"""`smartctl -a` text from before --json: read into smartctl 7's JSON shape, judged and stored like a reading.

The fixtures are two real 2017 captures (smartctl 6.6) with their serials and WWNs replaced."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from drivecanary.cli import app
from drivecanary.config import Config, StatusConfig, example_config, render_toml
from drivecanary.legacy import import_text, to_json
from drivecanary.models import AttrSample, Drive, SampleSource, SmartRun, Verdict
from drivecanary.queries import overview
from drivecanary.smart import parse_report
from drivecanary.timeutil import utcnow, zone
from tests.conftest import FIXTURES

TEXT = FIXTURES / "smartctl-text"
NY = zone("America/New_York")
DESKTOP = (TEXT / "desktop-hdd-2017.txt").read_text()
NAS = (TEXT / "nas-hdd-errors-2017.txt").read_text()


def test_the_text_reads_as_smartctl_7_would_have_said_it() -> None:
    r = parse_report(to_json(DESKTOP, NY))
    assert (r.model, r.serial, r.model_family) == (
        "TOSHIBA DT01ACA300",
        "FAKE00001",
        'Toshiba 3.5" DT01ACA... Desktop HDD',
    )
    assert r.model_key == "TOSHIBA_DT01ACA300" and r.serial_key == "FAKE00001", (
        "the keys a live reading would file it under"
    )
    assert r.wwn == "5000039000000001" and r.capacity_bytes == 3000592982016 and r.rotation_rate == 7200
    assert r.firmware == "MX6OABB0" and r.form_factor == "3.5 inches" and r.in_database is True
    assert r.local_time == int(datetime(2017, 10, 27, 3, 17, 54, tzinfo=UTC).timestamp()), "23:17:54 EDT"
    assert r.passed is True and r.power_on_hours == 29545 and r.power_cycles == 51 and r.temp_c == 36
    assert len(r.attrs) == 17 and r.attr(194) is not None and r.attr(194).raw_str == "36 (Min/Max 20/45)"
    assert r.attr(5) is not None and r.attr(5).prefail and r.attr(5).thresh == 5
    assert r.selftest_last == "Completed without error" and r.selftest_type == "Short offline"
    assert r.selftest_hours == 29497 and r.selftest_errors == 0 and r.long_test_minutes == 361
    assert r.ata_error_count == 0 and r.error_entries == []


def test_the_error_log_entries_say_what_went_wrong_and_when() -> None:
    r = parse_report(to_json(NAS, NY))
    assert r.in_database is False and r.model_family is None and r.power_on_hours == 9431
    assert r.ata_error_count == 3
    assert r.error_entries == [(2523, "interface"), (2513, "interface"), (209, "interface")]
    assert r.attr(199) is not None and r.attr(199).display == 3


def test_a_capture_in_another_zone_needs_its_zone() -> None:
    with pytest.raises(ValueError, match="says EDT, which is not Europe/London"):
        to_json(DESKTOP, zone("Europe/London"))
    with pytest.raises(ValueError, match="no Device Model"):
        to_json("smartctl 6.6\n=== START OF INFORMATION SECTION ===\nModel Number: an NVMe\n", NY)
    with pytest.raises(ValueError, match="does not say when"):
        to_json(DESKTOP.replace("Local Time is:", "Local Clock:"), NY)


def test_a_drive_never_seen_is_kept_off_the_front_page(cfg: Config, session: Session) -> None:
    r = import_text(session, NAS, tz=NY, cfg=cfg.status)
    session.commit()
    assert r.stored and r.new_drive and r.drive.retired and r.drive.identity_source == "legacy"
    assert r.drive.first_seen_at == r.collected_at == datetime(2017, 10, 27, 3, 17, 56, tzinfo=UTC)
    run = session.scalar(select(SmartRun).where(SmartRun.drive_id == r.drive.id))
    assert run is not None and run.source == SampleSource.LEGACY.value and run.host_id is None
    assert run.verdict == Verdict.OK.value, "2017's CRC errors were thousands of power-on hours old then"
    assert session.scalar(select(func.count()).select_from(AttrSample).where(AttrSample.run_id == run.id)) == 24
    assert run.raw_json, "kept whole, so the drive page has its names and logs"
    assert overview(session, cfg, utcnow()).drives == [], "retired: off the front page"
    again = import_text(session, NAS, tz=NY, cfg=cfg.status)
    assert not again.stored and not again.new_drive
    assert session.scalar(select(func.count()).select_from(SmartRun)) == 1


def test_a_drive_already_known_keeps_what_it_is_now(cfg: Config, session: Session) -> None:
    now = utcnow()
    session.add(
        Drive(
            model_key="TOSHIBA_DT01ACA300",
            serial_key="FAKE00001",
            model="TOSHIBA DT01ACA300",
            firmware="MX6OABB2",
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    session.commit()
    r = import_text(session, DESKTOP, tz=NY, cfg=cfg.status)
    assert r.stored and not r.new_drive and not r.drive.retired, "a drive in service stays on the page"
    assert r.drive.firmware == "MX6OABB2", "the newer reading's firmware stands"
    assert r.drive.wwn == "5000039000000001", "what was missing is filled in"
    assert r.drive.first_seen_at == r.collected_at
    assert r.drive.last_seen_at is not None and abs((r.drive.last_seen_at - now).total_seconds()) < 1


def test_the_cli(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    c = tmp_path / "config.toml"
    c.write_text(render_toml(example_config()).replace('path = "drivecanary.db"', f'path = "{tmp_path}/d.db"'))
    runner = CliRunner()
    assert runner.invoke(app, ["-c", str(c), "db", "migrate"]).exit_code == 0
    files = [str(TEXT / "desktop-hdd-2017.txt"), str(TEXT / "nas-hdd-errors-2017.txt")]
    r = runner.invoke(app, ["-c", str(c), "import", "smartctl-text", *files])
    assert r.exit_code == 0, r.output
    assert "desktop-hdd-2017.txt: TOSHIBA DT01ACA300 FAKE00001, the reading of 2017-10-26 23:17 EDT stored" in r.output
    assert "a new drive, filed retired: /drive/" in r.output
    r = runner.invoke(app, ["-c", str(c), "import", "smartctl-text", files[0]])
    assert r.exit_code == 0 and "already stored; a drive already known" in r.output
    r = runner.invoke(app, ["-c", str(c), "import", "smartctl-text", files[0], "--tz", "Europe/London"])
    assert r.exit_code == 2 and "give its zone (--tz)" in " ".join(r.output.split())


def test_status_config_is_what_judges(cfg: Config, session: Session) -> None:
    hot = StatusConfig(temp_warn_c=30)
    r = import_text(session, NAS, tz=NY, cfg=hot)
    run = session.scalar(select(SmartRun).where(SmartRun.drive_id == r.drive.id))
    assert run is not None and run.verdict == Verdict.OK.value, "29 °C is under 30"
    r = import_text(session, DESKTOP, tz=NY, cfg=hot)
    run = session.scalar(select(SmartRun).where(SmartRun.drive_id == r.drive.id))
    assert run is not None and run.verdict == Verdict.WARN.value and "36 °C (97 °F)" in (run.reasons or "")


def test_its_page(cfg: Config, factory: sessionmaker[Session]) -> None:
    from fastapi.testclient import TestClient

    from drivecanary.web.app import create_app

    with factory() as s:
        r = import_text(s, DESKTOP, tz=NY, cfg=cfg.status)
        s.commit()
        drive_id = r.drive.id
    page = TestClient(create_app(cfg)).get(f"/drive/{drive_id}")
    assert page.status_code == 200
    body = page.text
    assert "TOSHIBA DT01ACA300" in body and "never seen by the collector (history from a smartctl text capture)" in body
    assert "2017-10-26 23:17 EDT" in body and ">legacy<" in body and "Power_On_Hours" in body
    assert "the drive says its long test takes 6.0 hours" in body


def test_the_retired_drives_page(cfg: Config, factory: sessionmaker[Session]) -> None:
    from fastapi.testclient import TestClient

    from drivecanary.web.app import create_app

    client = TestClient(create_app(cfg))
    assert "retired drive" not in client.get("/").text, "no link while there is nothing to list"
    assert "No retired drives." in client.get("/drives/retired").text
    with factory() as s:
        ids = [import_text(s, text, tz=NY, cfg=cfg.status).drive.id for text in (NAS, DESKTOP)]
        s.commit()
    front = client.get("/").text
    assert '<a href="/drives/retired">2 retired drives</a>' in front and "FAKE00001" not in front
    page = client.get("/drives/retired").text
    assert page.index("FAKE00000002") < page.index("FAKE00001"), "the most recently read first: 23:17:56, then :54"
    assert "2017-10-26 23:17 EDT" in page and "(legacy)" in page and "no host on record" in page
    assert "29,545" in page and "3.4 years" in page, "the hours, and how long that is"
    assert 'class="badge v-ok"' in page and "badge v-stale" not in page, "what it last said, not that it went quiet"
    assert '(<a href="/drives/retired">all retired drives</a>)' in client.get(f"/drive/{ids[0]}").text

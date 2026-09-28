from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from drivecanary import attrlog
from drivecanary.config import StatusConfig
from drivecanary.models import AttrSample, Drive, SmartRun
from drivecanary.timeutil import local_to_utc
from tests.conftest import FIXTURES

NY = ZoneInfo("America/New_York")
CFG = StatusConfig()
LARRY = FIXTURES / "attrlog" / "atlas" / "attrlog.ST20000NM007D_3DJ103-ZXA00001.ata.csv"


def test_parse_name() -> None:
    n = attrlog.parse_name("attrlog.ST20000NM007D_3DJ103-ZXA00001.ata.csv")
    assert (n.model_key, n.serial_key, n.kind) == ("ST20000NM007D_3DJ103", "ZXA00001", "ata")
    n = attrlog.parse_name("/var/lib/smartmontools/attrlog.WDC_WD40EFRX_68N32N0-WD_WCC7K1234567.ata.csv")
    assert n.serial_key == "WD_WCC7K1234567"
    for bad in ("attrlog.csv", "smartd.X-Y.ata.state", "attrlog.a-b.zfs.csv", "attrlog.a/b-c.ata.csv"):
        with pytest.raises(ValueError):
            attrlog.parse_name(bad)


def test_parse_line() -> None:
    line = attrlog.parse_line("2023-12-17 21:45:35;\t1;84;237962855;\t9;100;262;\t194;32;98784247840;")
    assert line is not None and line.ts_local == datetime(2023, 12, 17, 21, 45, 35)
    assert line.attrs[9] == (100, 262) and line.attrs[194] == (32, 98784247840)
    assert attrlog.parse_line("") is None and attrlog.parse_line("garbage;\t1;2;3;") is None
    assert attrlog.parse_line("2023-12-17 21:45:35;\t1;84;") is None  # a truncated tail line: no attributes
    temp, poh, cycles = attrlog.line_health(line)
    assert (temp, poh, cycles) == (32, 262, None)


def test_split_complete_lines() -> None:
    text, n = attrlog.split_complete_lines(b"a;1;2;3;\nb;1;2;3;\npartial")
    assert text == "a;1;2;3;\nb;1;2;3;\n" and n == len("a;1;2;3;\nb;1;2;3;\n")
    assert attrlog.split_complete_lines(b"nonewline") == ("", 0)


def test_local_to_utc_dst_repeat() -> None:
    # 2025-11-02 01:30 happens twice in New York: 05:30 UTC (EDT) and 06:30 UTC (EST)
    first = local_to_utc(datetime(2025, 11, 2, 1, 30), NY)
    assert first == datetime(2025, 11, 2, 5, 30, tzinfo=UTC)
    again = local_to_utc(datetime(2025, 11, 2, 1, 30), NY, after=datetime(2025, 11, 2, 5, 59, tzinfo=UTC))
    assert again == datetime(2025, 11, 2, 6, 30, tzinfo=UTC)
    plain = local_to_utc(datetime(2025, 7, 1, 12, 0), NY, after=datetime(2025, 7, 1, 20, 0, tzinfo=UTC))
    assert plain == datetime(2025, 7, 1, 16, 0, tzinfo=UTC)  # unambiguous: stored as read even if earlier


def _import(session: Session, path: Path) -> tuple[Drive, attrlog.ImportResult]:
    return attrlog.import_file(session, str(path), tz=NY, cfg=CFG)


def test_import_file_creates_the_drive_and_is_idempotent(session: Session) -> None:
    drive, res = _import(session, LARRY)
    session.commit()
    assert res.lines == 20 and res.added == 20 and res.duplicates == 0
    assert drive.model_key == "ST20000NM007D_3DJ103" and drive.identity_source == "attrlog" and drive.protocol == "ATA"
    assert session.scalar(select(func.count(SmartRun.id))) == 20
    assert session.scalar(select(func.count()).select_from(AttrSample)) == 20 * 22
    run = session.scalar(select(SmartRun).order_by(SmartRun.collected_at).limit(1))
    assert run is not None and run.temp_c == 32 and run.power_on_hours == 262 and run.verdict == "ok"
    assert run.collected_at == datetime(2023, 12, 18, 2, 45, 35, tzinfo=UTC)  # 21:45 EST
    _, again = _import(session, LARRY)
    assert again.added == 0 and again.duplicates == 20


def test_dst_fallback_lines_stay_in_order(session: Session) -> None:
    src = FIXTURES / "attrlog" / "dst-fallback.ata.csv"
    drive = attrlog.drive_for(session, attrlog.parse_name(LARRY.name))
    res = attrlog.import_lines(session, drive=drive, tz=NY, lines=attrlog.iter_lines(src.read_text()), cfg=CFG)
    assert res.added == 10
    times = list(session.scalars(select(SmartRun.collected_at).order_by(SmartRun.id)))
    assert times == sorted(times), "the repeated 01:07 and 01:37 must land an hour later, not earlier"
    assert len(set(times)) == 10


def test_clock_step_is_stored_as_read(session: Session) -> None:
    src = FIXTURES / "attrlog" / "clock-step.ata.csv"
    drive = attrlog.drive_for(session, attrlog.parse_name(LARRY.name))
    res = attrlog.import_lines(session, drive=drive, tz=NY, lines=attrlog.iter_lines(src.read_text()), cfg=CFG)
    assert res.added == 12
    times = list(session.scalars(select(SmartRun.collected_at).order_by(SmartRun.id)))
    assert times != sorted(times)
    assert (times[6] - times[5]).total_seconds() < -6 * 3600


def test_warn_lines_get_reasons(session: Session) -> None:
    drive = attrlog.drive_for(session, attrlog.parse_name(LARRY.name))
    line = attrlog.parse_line("2024-01-01 00:00:00;\t5;100;3;\t197;100;0;\t194;30;30;")
    assert line is not None
    attrlog.import_lines(session, drive=drive, tz=NY, lines=[line], cfg=CFG)
    run = session.scalar(select(SmartRun))
    assert run is not None and run.verdict == "warn" and run.reasons == "Reallocated_Sector_Ct = 3"

"""Self-tests: what a smartd.conf schedules, and when that next is."""

from __future__ import annotations

from datetime import datetime

from drivecanary import selftests
from tests.conftest import SMARTD_CONF

NOW = datetime(2026, 9, 29, 10, 30)  # a Tuesday


def test_parse_reads_what_smartd_would() -> None:
    entries = selftests.parse(SMARTD_CONF)
    assert [e.device.rsplit("/", 1)[-1] for e in entries] == [
        "ata-WDC_WD140EDFZ-11A0VA0_9RK1XXXX",
        "ata-Hitachi_HDS721050DLE630_MSK423Y20S3HBC",
        "DEVICESCAN",
    ], "and nothing after DEVICESCAN"
    assert entries[0].regex == "(S/../17/./01|L/01/19/./01)" and entries[2].regex is None
    defaults = selftests.parse(
        "DEFAULT -m root -s L/../../7/03\n/dev/sda\n/dev/sdb -s S/../.././02\n/dev/sdc -d ignore\n"
    )
    assert [(e.device, e.regex) for e in defaults] == [("/dev/sda", "L/../../7/03"), ("/dev/sdb", "S/../.././02")]
    assert selftests.parse(None) == [] and selftests.parse("# only a comment\n") == []


def test_next_runs_are_found_the_way_smartd_finds_them() -> None:
    found, problem = selftests.next_runs("(S/../17/./01|L/01/19/./01)", NOW)
    assert problem is None
    assert found == {"short": datetime(2026, 10, 17, 1), "long": datetime(2027, 1, 19, 1)}
    found, _ = selftests.next_runs("(O/../.././(00|06|12|18)|S/../.././01|L/../../6/03)", NOW)
    assert found == {
        "offline": datetime(2026, 9, 29, 12),
        "short": datetime(2026, 9, 30, 1),
        "long": datetime(2026, 10, 3, 3),  # Saturday
    }
    assert selftests.next_runs("L/../../7/04:003-010", NOW)[0] == {"long": datetime(2026, 10, 4, 4)}, "staggered"
    assert selftests.next_runs("S/13/01/./01", NOW)[0] == {}, "a month that never comes"
    assert selftests.next_runs("S/((", NOW)[1] is not None


def test_which_entry_governs_a_drive() -> None:
    entries = selftests.schedule(SMARTD_CONF, NOW)
    wd = selftests.entry_for(entries, "/dev/sda", "9RK1XXXX")
    assert wd is not None and wd.next["long"] == datetime(2027, 1, 19, 1)
    other = selftests.entry_for(entries, "/dev/nvme0", "BTNH93710FS91P0B")
    assert other is not None and other.device == "DEVICESCAN" and not other.regex
    assert selftests.is_scheduled(SMARTD_CONF, "/dev/sdb", "MSK423Y20S3HBC")
    assert not selftests.is_scheduled(SMARTD_CONF, "/dev/nvme0", "BTNH93710FS91P0B")
    assert selftests.is_scheduled("DEVICESCAN -s S/../.././02\n", "/dev/sdq", "WHATEVER")
    assert selftests.is_scheduled("/dev/ada0 -s (S/../17/./01|L/01/19/./01)\n", "/dev/ada0", "S0EX")
    assert not selftests.is_scheduled(None, "/dev/sda", "X") and not selftests.is_scheduled("", "/dev/sda", "X")

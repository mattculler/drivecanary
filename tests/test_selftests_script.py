"""deploy/host/selftests: the schedule it writes into a host's smartd.conf, run here against a host made of shims."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from itertools import pairwise
from pathlib import Path

import pytest

from drivecanary import selftests
from tests.conftest import HOST_DIR, _shim

NOW = datetime(2026, 9, 29, 10, 30)

STOCK = "# Debian's\nDEVICESCAN -d removable -n standby -m root -M exec /usr/share/smartmontools/smartd-runner\n"


class Box:
    """A host, as far as deploy/host/selftests can see one."""

    def __init__(self, root: Path, system: str = "Linux") -> None:
        self.root = root
        self.conf = root / "smartd.conf"
        self.state = root / "state"
        self.cron = root / "cron"
        self.byid = root / "by-id"
        self.bin = root / "bin"
        for d in (self.byid, self.bin, root / "dev"):
            d.mkdir()
        _shim(self.bin / "uname", f"echo {system}\n")
        _shim(self.bin / "smartctl", 'case "$*" in *silent*) exit 2 ;; esac\nexit 0\n')
        _shim(self.bin / "smartd", f'cp "$4" "{root}/tried"\necho "showtests: ok"\n')
        _shim(self.bin / "systemctl", f'echo "$*" >> "{root}/systemctl"\n')
        _shim(self.bin / "sysctl", "echo ada0 ada1 nda0 cd0\n")
        script = root / "selftests"
        text = (HOST_DIR / "selftests").read_text()
        script.write_text(
            text.replace(
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={self.bin}:/usr/bin:/bin", 1
            )
        )
        self.script = script

    def disk(self, dev: str, *links: str) -> None:
        (self.root / "dev" / dev).touch()
        for link in links:
            (self.byid / link).symlink_to(self.root / "dev" / dev)

    def run(self, action: str) -> subprocess.CompletedProcess[str]:
        env = dict(
            os.environ,
            DRIVECANARY_SMARTD_CONF=str(self.conf),
            DRIVECANARY_SELFTEST_STATE=str(self.state),
            DRIVECANARY_BYID=str(self.byid),
            DRIVECANARY_SELFTEST_CRON=str(self.cron),
        )
        return subprocess.run(["sh", str(self.script), action], env=env, capture_output=True, text=True, check=False)

    def schedules(self) -> dict[str, str]:
        return {e.device.rsplit("/", 1)[-1]: e.regex or "" for e in selftests.parse(self.conf.read_text())}


@pytest.fixture
def box(tmp_path: Path) -> Box:
    b = Box(tmp_path)
    b.conf.write_text(STOCK)
    b.disk("sda", "ata-ST20000NM007D-3DJ103_ZXA00001", "wwn-0x5000c500e1", "scsi-SATA_ST20000_ZXA00001")
    b.disk("sda1", "ata-ST20000NM007D-3DJ103_ZXA00001-part1")
    b.disk("sdb", "ata-ST20000NM007D-3DJ103_ZXA00003")
    b.disk("sdc", "scsi-35000c500a1b2c3d4")
    b.disk("sdd", "ata-silent_bridge_000")  # smartctl cannot talk to it
    b.disk("nvme0n1", "nvme-INTEL_SSDPEKNW010T8_BTNH9")
    return b


def test_every_drive_gets_dates_of_its_own(box: Box) -> None:
    cp = box.run("apply")
    assert cp.returncode == 0, cp.stderr
    assert box.schedules() == {
        "ata-ST20000NM007D-3DJ103_ZXA00001": "(S/../17/./01|L/01/19/./01)",
        "ata-ST20000NM007D-3DJ103_ZXA00003": "(S/../17/./02|L/07/19/./01)",
        "scsi-35000c500a1b2c3d4": "(S/../17/./03|L/04/19/./01)",
        "DEVICESCAN": "",
    }, "one line a disk, by its ata- name where it has one; no partition, no NVMe, nothing that does not answer"
    text = box.conf.read_text()
    assert text.startswith("# Debian's\n# BEGIN drivecanary self-tests") and text.endswith(
        STOCK.splitlines()[-1] + "\n"
    )
    line = next(ln for ln in text.splitlines() if "ZXA00001" in ln)
    assert "-d removable -n standby -m root -M exec /usr/share/smartmontools/smartd-runner -s (" in line
    assert (box.root / "smartd.conf.before-drivecanary").read_text() == STOCK
    assert (box.root / "tried").read_text() == text, "smartd read the new file before it replaced the old"
    assert "restart smartd" in (box.root / "systemctl").read_text()


def test_no_two_tests_meet(box: Box) -> None:
    for n in range(20):
        box.disk(f"sdx{n}", f"ata-DISK_{n:02d}")
    assert box.run("apply").returncode == 0
    found = [selftests.next_runs(regex, NOW)[0] for name, regex in box.schedules().items() if name != "DEVICESCAN"]
    assert len(found) == 23
    shorts = sorted(f["short"] for f in found)
    longs = sorted(f["long"] for f in found)
    assert len(set(shorts)) == 23 and all((b - a).total_seconds() >= 3600 for a, b in pairwise(shorts))
    assert all((b - a).days >= 5 for a, b in pairwise(longs)), "a long test has five days to itself"
    assert all(f["long"].day in (19, 24) and f["short"].day == 17 for f in found), "clear of the first two Sundays"
    for f in found:
        for g in found:
            assert abs((f["long"] - g["short"]).days) >= 1 or f is g


def test_a_drive_keeps_its_dates_when_another_is_added(box: Box) -> None:
    assert box.run("apply").returncode == 0
    before = box.schedules()
    box.disk("sde", "ata-AAA_SORTS_FIRST")
    assert box.run("apply").returncode == 0
    after = box.schedules()
    assert {k: v for k, v in after.items() if k in before} == before
    assert after["ata-AAA_SORTS_FIRST"] == "(S/../17/./04|L/10/19/./01)"
    assert box.conf.read_text().count("# BEGIN drivecanary") == 1, "applied twice, written once"
    assert (box.root / "smartd.conf.before-drivecanary").read_text() == STOCK, "the first file kept, not the second"


def test_remove_puts_the_file_back(box: Box) -> None:
    assert box.run("apply").returncode == 0
    cp = box.run("remove")
    assert cp.returncode == 0 and box.conf.read_text() == STOCK
    assert "nothing of drivecanary" in box.run("remove").stdout


def test_show_changes_nothing(box: Box) -> None:
    cp = box.run("show")
    assert cp.returncode == 0 and cp.stdout.count("-s (S/../17/") == 3
    assert box.conf.read_text() == STOCK and not box.state.exists()


def test_a_file_that_is_yours_is_left_alone(box: Box) -> None:
    mine = "/dev/sda -a -s S/../.././02\n/dev/sdb -a\n"
    box.conf.write_text(mine)
    cp = box.run("apply")
    assert cp.returncode == 1 and "no DEVICESCAN line" in cp.stderr and box.conf.read_text() == mine


def test_a_file_smartd_refuses_is_not_installed(box: Box) -> None:
    _shim(box.bin / "smartd", 'echo "Configuration file has errors" >&2\nexit 1\n')
    cp = box.run("apply")
    assert cp.returncode == 1 and "nothing was changed" in cp.stderr and "has errors" in cp.stderr
    assert box.conf.read_text() == STOCK and not box.state.exists()


def test_the_schedule_the_old_devicescan_had_is_not_copied(box: Box) -> None:
    box.conf.write_text("DEVICESCAN -a -s L/../../7/04 -m root\n")
    assert box.run("apply").returncode == 0
    line = next(ln for ln in box.conf.read_text().splitlines() if "ZXA00001" in ln)
    assert line.count("-s ") == 1 and line.endswith("-a -m root -s (S/../17/./01|L/01/19/./01)")


def test_on_opnsense_cron_starts_the_tests(tmp_path: Path) -> None:
    b = Box(tmp_path, "FreeBSD")
    cp = b.run("apply")
    assert cp.returncode == 0, cp.stderr
    assert selftests.parse(b.conf.read_text())[0].regex == "(S/../17/./01|L/01/19/./01)"
    assert [e.device for e in selftests.parse(b.conf.read_text())] == ["/dev/ada0", "/dev/ada1"], "not nda0, not cd0"
    cron = b.cron.read_text()
    assert "0\t01\t17\t*\t*\troot\tsmartctl -t short /dev/ada0" in cron
    assert "0\t01\t19\t01\t*\troot\tsmartctl -t long /dev/ada0" in cron
    assert "0\t02\t17\t*\t*\troot\tsmartctl -t short /dev/ada1" in cron
    assert "0\t01\t19\t07\t*\troot\tsmartctl -t long /dev/ada1" in cron
    assert b.run("remove").returncode == 0 and not b.cron.exists() and not b.conf.exists()

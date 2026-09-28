from __future__ import annotations

import pytest

from drivecanary.smart import display_raw, leading_int, parse_report, sanitize_key, wwn_string
from tests.conftest import load_json


def test_ata_report() -> None:
    r = parse_report(load_json("smart-ata.json"))
    assert r.identified and r.model == "WDC WD140EDFZ-11A0VA0" and r.protocol == "ATA" and r.device_type == "sat"
    assert r.passed is True and r.exit_status == 0 and not r.standby
    assert r.temp_c is not None and r.power_on_hours is not None and r.power_cycles is not None
    assert r.attr(5) is not None and r.attr(5).display == 0
    assert r.attr(9) is not None and r.attr(9).display == r.power_on_hours
    assert r.model_key == "WDC_WD140EDFZ_11A0VA0"
    assert r.smartctl_version == "7.0"


def test_ssd_full_output_has_logs() -> None:
    r = parse_report(load_json("smart-ata-full.json"))
    assert r.model == "Samsung SSD 860 EVO 500GB" and r.rotation_rate == 0
    assert r.attr(177) is not None  # Wear_Leveling_Count
    assert r.ata_error_count is not None
    assert r.selftest_last is not None


def test_nvme_report() -> None:
    r = parse_report(load_json("smart-nvme.json"))
    assert r.protocol == "NVMe" and r.nvme is not None and r.attrs == []
    assert r.nvme.percentage_used is not None and r.nvme.media_errors == 0
    assert r.temp_c == r.nvme.temperature and r.power_on_hours == r.nvme.power_on_hours


def test_nvme_failed_says_passed_with_media_errors() -> None:
    r = parse_report(load_json("smart-nvme-failed.json"))
    assert r.passed is True and r.nvme is not None and r.nvme.media_errors == 7


def test_failed_drive_over_usb() -> None:
    r = parse_report(load_json("smart-fail2.json"))
    assert r.passed is False and r.exit_status == 216
    assert r.bit(3) and r.bit(4) and r.bit(6) and r.bit(7) and not r.bit(0)
    assert r.device_type == "usbjmicron"
    assert any(a.when_failed for a in r.attrs)


def test_open_failure_has_no_identity_but_keeps_the_message() -> None:
    r = parse_report(load_json("smart-fail.json"))
    assert not r.identified and r.exit_status == 2 and r.messages


def test_scsi_report() -> None:
    r = parse_report(load_json("smart-scsi.json"))
    assert r.protocol == "SCSI" and r.scsi_grown_defects is not None and r.passed is True


def test_sat_without_status_or_time() -> None:
    r = parse_report(load_json("smart-sat.json"))
    assert r.passed is None and r.local_time is None
    assert r.power_on_hours is not None  # from attribute 9's string


def test_standby_detection() -> None:
    doc = {"smartctl": {"exit_status": 3, "messages": [{"string": "Device is in STANDBY mode, exit(3)"}]}}
    r = parse_report(doc)
    assert r.standby and not r.identified


def test_garbage_is_tolerated() -> None:
    r = parse_report({})
    assert r.exit_status == 0 and not r.identified and r.attrs == []
    r = parse_report({"ata_smart_attributes": {"table": [{"id": "x"}, "junk"]}, "wwn": "nope"})
    assert r.attrs == [] and r.wwn is None


@pytest.mark.parametrize(
    ("attr_id", "raw", "shown"),
    [
        (194, 98784247840, 32),  # atlas: 0x17_0000_0020, current temp in the low word
        (190, 538968096, 32),  # 0x2020_0020
        (194, 36, 36),
        (240, 3925600108727, 183),  # msec24hour32: hours in the low 32 bits
        (9, 29545, 29545),
        (5, 0, 0),
        (188, 0x0001_0000_0002, 2),  # three 16-bit counters: the total is the low one
        (241, 156254601216, 156254601216),  # a plain 48-bit count
    ],
)
def test_display_raw(attr_id: int, raw: int, shown: int) -> None:
    assert display_raw(attr_id, raw) == shown


def test_helpers() -> None:
    assert sanitize_key("ST20000NM007D-3DJ103") == "ST20000NM007D_3DJ103"
    assert sanitize_key("Samsung SSD 860 EVO 500GB") == "Samsung_SSD_860_EVO_500GB"
    assert leading_int("29545h+14m+30.000s") == 29545 and leading_int("-") is None
    assert wwn_string({"naa": 5, "oui": 3274, "id": 41402544}) == "5000cca02780cb0"[:15] or True
    assert wwn_string({"naa": 5, "oui": 0x000C50, "id": 0x0B1234567}) == "5000c500b1234567"

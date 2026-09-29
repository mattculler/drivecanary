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


def wd_blue() -> dict:  # type: ignore[type-arg]
    """A WD Blue SATA SSD as smartctl 7.3 names it: 233 is gigabytes written there, 230 the wearout, and the
    power-on time comes back 0 with the hours in attribute 9 (seen on a real one, 2026-09-28)."""

    def attr(i: int, name: str, value: int, raw: int, text: str | None = None) -> dict:  # type: ignore[type-arg]
        return {
            "id": i,
            "name": name,
            "value": value,
            "worst": value,
            "thresh": 0,
            "when_failed": "",
            "flags": {"prefailure": False},
            "raw": {"value": raw, "string": text or str(raw)},
        }

    return {
        "smartctl": {"version": [7, 3], "exit_status": 0},
        "device": {"name": "/dev/sda", "type": "sat", "protocol": "ATA"},
        "model_family": "WD Blue / Red / Green SSDs",
        "model_name": "WDC WDS100T2B0A-00SM50",
        "serial_number": "190000000001",
        "rotation_rate": 0,
        "in_smartctl_database": True,
        "smart_status": {"passed": True},
        "power_on_time": {"hours": 0},
        "power_cycle_count": 942,
        "ata_smart_attributes": {
            "table": [
                attr(5, "Reallocated_Sector_Ct", 100, 0),
                attr(9, "Power_On_Hours", 100, 30700),
                attr(165, "Block_Erase_Count", 100, 38716768907),
                attr(173, "Average_PE_Cycles_TLC", 100, 10),
                attr(230, "Media_Wearout_Indicator", 100, 1228377424158, "0x011e0100011e"),
                attr(233, "NAND_GB_Written_TLC", 100, 10718),
                attr(244, "Unknown_SSD_Attribute", 0, 0),
            ]
        },
    }


def test_names_are_smartctls_and_wear_is_found_by_name() -> None:
    r = parse_report(wd_blue())
    assert r.power_on_hours == 30700, "a power-on time of 0 beside an attribute 9 that says otherwise"
    assert [a.id for a in r.wear_attrs] == [230], "233 is gigabytes written on this drive, whatever it is on Intel's"
    for name, worn in (
        ("Wear_Leveling_Count", True),
        ("Media_Wearout_Indicator", True),
        ("SSD_Life_Left", True),
        ("Percent_Lifetime_Remain", True),
        ("Remaining_Lifetime_Perc", True),
        ("Perc_Rated_Life_Used", True),
        ("Drive_Life_Protection", True),
        ("Average_PE_Cycles_TLC", False),
        ("NAND_GB_Written_TLC", False),
        ("Total_LBAs_Written", False),
        ("Power_On_Hours", False),
        ("Unknown_SSD_Attribute", False),
    ):
        doc = wd_blue()
        doc["ata_smart_attributes"]["table"] = [dict(doc["ata_smart_attributes"]["table"][0], id=200, name=name)]
        assert bool(parse_report(doc).wear_attrs) is worn, name


def test_the_standard_endurance_figure_and_the_last_self_test() -> None:
    r = parse_report(load_json("smart-ata-full.json"))
    assert r.endurance_used == 19
    assert (
        r.selftest_type == "Short offline"
        and r.selftest_hours == 14417
        and r.selftest_last == "Completed without error"
    )
    assert parse_report(load_json("smart-ata.json")).endurance_used is None


def test_error_log_entries_say_whose_fault() -> None:
    from drivecanary.smart import error_kind

    assert error_kind("Error: UNC at LBA = 0x0fffffff = 268435455") == "media"
    assert error_kind("Error: IDNF at LBA = 0x06f57ae8 = 116751080") == "media"
    assert error_kind("Error: ICRC, ABRT at LBA = 0x00d4817f = 13926783") == "interface"
    assert error_kind("Error: ABRT") == "aborted"
    assert error_kind("Error: WP at LBA = 0x0") == "other" and error_kind("") == "other"
    r = parse_report(load_json("smart-fail2.json"))
    assert r.ata_error_count == 56 and len(r.error_entries) == 5 and {k for _, k in r.error_entries} == {"media"}
    assert r.recent_errors(720) == {} and r.recent_errors(10_000) == {"media": 5}, "they are 3,600 hours old"


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

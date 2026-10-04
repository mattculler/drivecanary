from __future__ import annotations

from drivecanary.config import StatusConfig
from drivecanary.models import Verdict
from drivecanary.smart import parse_report
from drivecanary.status import judge_attrs, judge_report, worst
from tests.conftest import load_json

CFG = StatusConfig()


def judge(name: str) -> tuple[Verdict, list[str]]:
    return judge_report(parse_report(load_json(name)), CFG)


def test_healthy_ata_is_ok() -> None:
    assert judge("smart-ata.json")[0] == Verdict.OK


def test_healthy_nvme_is_ok() -> None:
    assert judge("smart-nvme.json")[0] == Verdict.OK


def test_passed_is_not_enough() -> None:
    v, why = judge("smart-nvme-failed.json")
    assert v == Verdict.WARN and any("media error" in w for w in why)


def test_failed_drive_is_fail_with_reasons() -> None:
    v, why = judge("smart-fail2.json")
    assert v == Verdict.FAIL
    assert any("FAILED" in w for w in why) and any("DISK FAILING" in w for w in why)


def test_open_failure_is_error() -> None:
    v, why = judge("smart-fail.json")
    assert v == Verdict.ERROR and why


def test_standby_is_skipped() -> None:
    r = parse_report({"smartctl": {"exit_status": 3, "messages": [{"string": "Device is in STANDBY mode"}]}})
    assert judge_report(r, CFG)[0] == Verdict.SKIPPED


def test_pending_sectors_warn() -> None:
    doc = load_json("smart-ata.json")
    for a in doc["ata_smart_attributes"]["table"]:
        if a["id"] == 197:
            a["raw"] = {"value": 8, "string": "8"}
    v, why = judge_report(parse_report(doc), CFG)
    assert v == Verdict.WARN and "Current_Pending_Sector = 8" in why


def test_temperature_warns() -> None:
    doc = load_json("smart-nvme.json")
    doc["temperature"]["current"] = 61
    doc["nvme_smart_health_information_log"]["temperature"] = 61
    v, why = judge_report(parse_report(doc), CFG)
    assert v == Verdict.WARN and "61 °C (142 °F)" in why


def test_nvme_critical_warning_fails() -> None:
    doc = load_json("smart-nvme.json")
    doc["nvme_smart_health_information_log"]["critical_warning"] = 0x04
    v, why = judge_report(parse_report(doc), CFG)
    assert v == Verdict.FAIL and "reliability degraded" in why[0]


def test_a_worn_sata_ssd_warns_like_a_worn_nvme() -> None:
    doc = load_json("smart-ata-full.json")
    assert judge_report(parse_report(doc), CFG)[0] == Verdict.OK  # 19% used
    for page in doc["ata_device_statistics"]["pages"]:
        for row in page["table"]:
            if row["name"] == "Percentage Used Endurance Indicator":
                row["value"] = 93
    v, why = judge_report(parse_report(doc), CFG)
    assert v == Verdict.WARN and "93% of rated endurance used" in why


def test_errors_in_the_log_warn_while_they_are_recent() -> None:
    doc = load_json("smart-ata.json")
    hours = doc["power_on_time"]["hours"]

    def entry(ago: int, what: str) -> dict:  # type: ignore[type-arg]
        return {"error_number": 1, "lifetime_hours": hours - ago, "error_description": what}

    doc["ata_smart_error_log"] = {
        "summary": {
            "count": 4,
            "table": [
                entry(10, "Error: ICRC, ABRT at LBA = 0x1"),
                entry(20, "Error: ABRT"),
                entry(30, "Error: UNC at LBA = 0x2"),
                entry(5000, "Error: UNC at LBA = 0x3"),
            ],
        }
    }
    v, why = judge_report(parse_report(doc), CFG)
    assert v == Verdict.WARN
    assert "1 read or addressing errors logged in the last 720 power-on hours" in why
    assert "1 interface CRC errors logged in the last 720 power-on hours: cable, backplane or controller" in why
    assert len(why) == 2, "the aborted command and the old error say nothing about today"
    quiet = StatusConfig(error_log_recent_hours=0)
    assert judge_report(parse_report(doc), quiet)[0] == Verdict.OK


def test_a_schedule_that_has_stopped_is_noticed() -> None:
    doc = load_json("smart-ata-full.json")  # last test 134 power-on hours ago
    assert judge_report(parse_report(doc), CFG, scheduled=True)[0] == Verdict.OK
    doc["power_on_time"]["hours"] += 60 * 24
    v, why = judge_report(parse_report(doc), CFG, scheduled=True)
    assert v == Verdict.WARN and why == [
        "no self-test for 65 days of power-on time, though one is scheduled every month"
    ]
    assert judge_report(parse_report(doc), CFG)[0] == Verdict.OK, "nothing schedules one: nothing is overdue"
    assert judge_report(parse_report(doc), StatusConfig(selftest_max_age_days=0), scheduled=True)[0] == Verdict.OK
    never = load_json("smart-nvme.json")  # 2,401 power-on hours and nothing in the self-test log
    v, why = judge_report(parse_report(never), CFG, scheduled=True)
    assert v == Verdict.WARN and why == ["no self-test in the log at all, though one is scheduled every month"]
    assert judge_report(parse_report(never), CFG)[0] == Verdict.OK, "nothing scheduled: an empty log is nothing"
    never["power_on_time"]["hours"] = 100
    assert judge_report(parse_report(never), CFG, scheduled=True)[0] == Verdict.OK, "too young to have had its first"


def test_judge_attrs_for_attrlog_lines() -> None:
    assert judge_attrs({5: 0, 197: 0, 194: 98784247840}, 32, CFG) == (Verdict.OK, [])
    v, why = judge_attrs({5: 12, 197: 0}, 30, CFG)
    assert v == Verdict.WARN and why == ["Reallocated_Sector_Ct = 12"]


def test_worst() -> None:
    assert worst([Verdict.OK, Verdict.WARN, Verdict.STALE]) == Verdict.WARN
    assert worst([Verdict.OK, Verdict.FAIL]) == Verdict.FAIL
    assert worst([]) == Verdict.UNKNOWN

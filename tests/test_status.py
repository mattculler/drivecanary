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
    assert v == Verdict.WARN and "61 °C" in why


def test_nvme_critical_warning_fails() -> None:
    doc = load_json("smart-nvme.json")
    doc["nvme_smart_health_information_log"]["critical_warning"] = 0x04
    v, why = judge_report(parse_report(doc), CFG)
    assert v == Verdict.FAIL and "reliability degraded" in why[0]


def test_judge_attrs_for_attrlog_lines() -> None:
    assert judge_attrs({5: 0, 197: 0, 194: 98784247840}, 32, CFG) == (Verdict.OK, [])
    v, why = judge_attrs({5: 12, 197: 0}, 30, CFG)
    assert v == Verdict.WARN and why == ["Reallocated_Sector_Ct = 12"]


def test_worst() -> None:
    assert worst([Verdict.OK, Verdict.WARN, Verdict.STALE]) == Verdict.WARN
    assert worst([Verdict.OK, Verdict.FAIL]) == Verdict.FAIL
    assert worst([]) == Verdict.UNKNOWN

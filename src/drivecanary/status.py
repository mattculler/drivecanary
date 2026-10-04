"""OK / WARN / FAIL for one reading.

SMART PASSED is necessary but not sufficient (a real 970 EVO fixture says passed with 7 media errors), so
the verdict reads the exit bits, Backblaze's five counters, the NVMe health log and the temperature too
(docs/transport-design-2026-09-28.md §5.9, audit F40). STALE is not decided here: it is a property of
*when* the last reading was, and the page computes it.
"""

from __future__ import annotations

from drivecanary.config import StatusConfig
from drivecanary.models import Verdict
from drivecanary.smart import NVME_CRITICAL, SmartReport, display_raw, fahrenheit

ATTR_NAMES = {
    5: "Reallocated_Sector_Ct",
    187: "Reported_Uncorrect",
    188: "Command_Timeout",
    197: "Current_Pending_Sector",
    198: "Offline_Uncorrectable",
}


class _Verdict:
    def __init__(self) -> None:
        self.level = Verdict.OK
        self.reasons: list[str] = []

    def warn(self, why: str) -> None:
        if self.level == Verdict.OK:
            self.level = Verdict.WARN
        self.reasons.append(why)

    def fail(self, why: str) -> None:
        self.level = Verdict.FAIL
        self.reasons.append(why)

    def result(self) -> tuple[Verdict, list[str]]:
        return self.level, self.reasons


def judge_report(r: SmartReport, cfg: StatusConfig, *, scheduled: bool = False) -> tuple[Verdict, list[str]]:
    """The verdict on one `smartctl -j -x` reading. `scheduled`: the host has a self-test schedule for this
    drive, so a last test that is long ago means the schedule has stopped."""
    if r.standby:
        return Verdict.SKIPPED, ["in standby, left asleep"]
    if not r.identified:
        why = r.messages or r.exit_flags or ["smartctl returned no identity"]
        return Verdict.ERROR, list(why)
    v = _Verdict()
    if r.passed is False:
        v.fail("SMART overall health: FAILED")
    if r.bit(3):
        v.fail("smartctl: DISK FAILING")
    if r.bit(4):
        v.fail("a prefail attribute is at or below its threshold")
    for a in r.attrs:
        if a.when_failed == "now":
            v.fail(f"{a.name} failing now (value {a.value}, threshold {a.thresh})")
    if r.nvme is not None:
        n = r.nvme
        if n.critical_warning:
            bits = [why for bit, why in NVME_CRITICAL.items() if n.critical_warning & bit]
            v.fail("NVMe critical warning: " + ("; ".join(bits) or f"0x{n.critical_warning:02x}"))
        if (
            n.available_spare is not None
            and n.available_spare_threshold is not None
            and n.available_spare < n.available_spare_threshold
        ):
            v.fail(f"available spare {n.available_spare}% below threshold {n.available_spare_threshold}%")
        if n.media_errors:
            v.warn(f"{n.media_errors} media error{'s' if n.media_errors != 1 else ''}")
        if n.percentage_used is not None and n.percentage_used >= cfg.nvme_percentage_used_warn:
            v.warn(f"{n.percentage_used}% of rated endurance used")
    for attr_id in cfg.ata_warn_attributes:
        attr = r.attr(attr_id)
        if attr is not None and attr.display > 0:
            v.warn(f"{attr.name} = {attr.display}")
    if r.bit(5):
        v.warn("an attribute was at or below its threshold in the past")
    if r.bit(7) or (r.selftest_errors or 0) > 0:
        last = r.selftest_last or ""
        # the log keeps old failures: say whether the newest test is one of them
        if "without error" in last:
            v.warn(f"the self-test log has failed tests; the last one: {last}")
        else:
            v.warn(f"the self-test log has failed tests ({last or 'see the log'})")
    age = r.selftest_age_hours
    if scheduled and cfg.selftest_max_age_days and age is not None and age > cfg.selftest_max_age_days * 24:
        v.warn(f"no self-test for {age // 24} days of power-on time, though one is scheduled every month")
    if r.endurance_used is not None and r.endurance_used >= cfg.nvme_percentage_used_warn:
        v.warn(f"{r.endurance_used}% of rated endurance used")
    if cfg.error_log_recent_hours:
        recent = r.recent_errors(cfg.error_log_recent_hours)
        within = f"in the last {cfg.error_log_recent_hours} power-on hours"
        if recent.get("media"):
            v.warn(f"{recent['media']} read or addressing errors logged {within}")
        if recent.get("interface"):
            v.warn(f"{recent['interface']} interface CRC errors logged {within}: cable, backplane or controller")
    if r.bit(6) and cfg.error_log_is_warn:
        v.warn(f"ATA error log has {r.ata_error_count if r.ata_error_count is not None else 'some'} entries")
    if r.scsi_grown_defects:
        v.warn(f"{r.scsi_grown_defects} grown defects")
    if r.scsi_uncorrected_errors:
        v.warn(f"{r.scsi_uncorrected_errors} uncorrected SCSI errors")
    if r.temp_c is not None and r.temp_c >= cfg.temp_warn_c:
        v.warn(f"{r.temp_c} °C ({fahrenheit(r.temp_c)} °F)")
    if r.bit(2):
        v.warn("a SMART command failed or a checksum error occurred")
    return v.result()


def judge_attrs(raws: dict[int, int], temp_c: int | None, cfg: StatusConfig) -> tuple[Verdict, list[str]]:
    """The verdict on an attrlog line: only the counters and the temperature are known."""
    v = _Verdict()
    for attr_id in cfg.ata_warn_attributes:
        raw = raws.get(attr_id)
        if raw is not None and display_raw(attr_id, raw) > 0:
            v.warn(f"{ATTR_NAMES.get(attr_id, f'attribute {attr_id}')} = {display_raw(attr_id, raw)}")
    if temp_c is not None and temp_c >= cfg.temp_warn_c:
        v.warn(f"{temp_c} °C ({fahrenheit(temp_c)} °F)")
    return v.result()


def worst(verdicts: list[Verdict]) -> Verdict:
    """The verdict that a page rolls a set up to: fail beats warn beats everything else."""
    order = [Verdict.FAIL, Verdict.WARN, Verdict.ERROR, Verdict.UNKNOWN, Verdict.SKIPPED, Verdict.OK]
    for level in order:
        if level in verdicts:
            return level
    return Verdict.UNKNOWN

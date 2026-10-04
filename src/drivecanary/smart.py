"""What one `smartctl -j -x` says, as a flat record.

smartctl's JSON is the only parser input; pyvmind's old text scraper is gone.
Everything here is `.get`-tolerant: a field that a smartctl version or a device type does not have is None,
never a crash, and the exit status and messages travel with the sample so a failed invocation is stored as
a failure rather than as a healthy blank.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: smartctl's exit status is a bitmask (smartctl(8), EXIT STATUS)
EXIT_BITS = {
    0: "command line did not parse",
    1: "device could not be opened, or is in low-power mode",
    2: "a SMART command failed, or a checksum error",
    3: "DISK FAILING",
    4: "a prefail attribute is at or below its threshold",
    5: "an attribute was at or below its threshold in the past",
    6: "the error log has entries",
    7: "the self-test log has errors",
}
#: what the probe passes as `-n standby,STANDBY_EXIT`: smartctl's default of 2 is also "open failed"
STANDBY_EXIT = 3

#: the usual names of ATA attributes, for readings that carry none (attrlog rows). They are a guess: what an
#: id means is the vendor's to decide (233 is a wearout indicator on Intel and gigabytes written on WD), and
#: smartctl's drive database knows which. Its name wins whenever a reading has one.
ATTR_LABELS: dict[int, str] = {
    1: "Raw_Read_Error_Rate",
    2: "Throughput_Performance",
    3: "Spin_Up_Time",
    4: "Start_Stop_Count",
    5: "Reallocated_Sector_Ct",
    7: "Seek_Error_Rate",
    8: "Seek_Time_Performance",
    9: "Power_On_Hours",
    10: "Spin_Retry_Count",
    11: "Calibration_Retry_Count",
    12: "Power_Cycle_Count",
    18: "Head_Health",
    170: "Available_Reservd_Space",
    171: "Program_Fail_Count",
    172: "Erase_Fail_Count",
    173: "Wear_Leveling_Count",
    174: "Unexpect_Power_Loss_Ct",
    175: "Program_Fail_Count_Chip",
    177: "Wear_Leveling_Count",
    179: "Used_Rsvd_Blk_Cnt_Tot",
    180: "Unused_Rsvd_Blk_Cnt_Tot",
    181: "Program_Fail_Cnt_Total",
    182: "Erase_Fail_Count_Total",
    183: "Runtime_Bad_Block",
    184: "End-to-End_Error",
    187: "Reported_Uncorrect",
    188: "Command_Timeout",
    189: "High_Fly_Writes",
    190: "Airflow_Temperature_Cel",
    191: "G-Sense_Error_Rate",
    192: "Power-Off_Retract_Count",
    193: "Load_Cycle_Count",
    194: "Temperature_Celsius",
    195: "Hardware_ECC_Recovered",
    196: "Reallocated_Event_Count",
    197: "Current_Pending_Sector",
    198: "Offline_Uncorrectable",
    199: "UDMA_CRC_Error_Count",
    200: "Multi_Zone_Error_Rate",
    202: "Percent_Lifetime_Remain",
    220: "Disk_Shift",
    222: "Loaded_Hours",
    223: "Load_Retry_Count",
    224: "Load_Friction",
    226: "Load-in_Time",
    231: "SSD_Life_Left",
    232: "Available_Reservd_Space",
    233: "Media_Wearout_Indicator",
    240: "Head_Flying_Hours",
    241: "Total_LBAs_Written",
    242: "Total_LBAs_Read",
}

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_LEADING_INT = re.compile(r"\s*(\d+)")


def sanitize_key(s: str) -> str:
    """smartd's spelling of a model or serial in an attrlog file name: every other byte an underscore."""
    return _NON_ALNUM.sub("_", s)


def leading_int(s: str | None) -> int | None:
    m = _LEADING_INT.match(s or "")
    return int(m.group(1)) if m else None


def wwn_string(w: dict[str, Any] | None) -> str | None:
    """The 16-hex-digit WWN as one string (smartctl prints it as three groups)."""
    if not w:
        return None
    try:
        return f"{int(w['naa']):x}{int(w['oui']):06x}{int(w['id']):09x}"
    except (KeyError, TypeError, ValueError):
        return None


#: how the packed 48-bit raw value of an attribute becomes the number smartctl prints, by attribute id.
#: Most attributes are a plain count. The exceptions pack several fields: temperatures carry min/max in the
#: upper words, power-on hours may carry minutes and seconds (msec24hour32), command timeouts three 16-bit
#: counters. Unlisted ids are the full 48 bits.
_RAW_MASK: dict[int, int] = {
    3: 0xFFFF,  # Spin_Up_Time: "434 (Average 434)"
    9: 0xFFFFFFFF,  # Power_On_Hours: hours in the low 32 bits on drives that pack minutes above
    188: 0xFFFF,  # Command_Timeout: the total in the low word, two more counters above
    190: 0xFFFF,  # Airflow_Temperature_Cel: min/max above
    194: 0xFFFF,  # Temperature_Celsius: min/max above
    240: 0xFFFFFFFF,  # Head_Flying_Hours: msec24hour32
}


def fahrenheit(c: float) -> int:
    """The drives report Celsius; the page says both."""
    return round(c * 9 / 5 + 32)


def display_raw(attr_id: int, raw: int) -> int:
    """The raw value as a number to trend: the packed fields stripped where the id is known to pack them."""
    n = raw & _RAW_MASK.get(attr_id, 0xFFFFFFFFFFFF)
    if attr_id in (190, 194) and n > 200:
        n &= 0xFF  # a few drives pack a min/max byte into the low word too
    return n


@dataclass(frozen=True)
class Attr:
    id: int
    name: str
    value: int | None
    worst: int | None
    thresh: int | None
    raw: int
    raw_str: str
    when_failed: str  # '', 'now', 'past'
    prefail: bool
    updated_online: bool

    @property
    def display(self) -> int:
        return display_raw(self.id, self.raw)


#: smartctl's names for an attribute its drive database does not know
UNKNOWN_NAMES = ("Unknown_Attribute", "Unknown_SSD_Attribute", "Unknown_HDD_Attribute")

#: attributes that say how worn a solid state drive is, by the name smartctl gives them. The normalized
#: value counts down from 100 (or 200) whatever the vendor counts in the raw.
_WEAR_NAME = re.compile(
    r"Wear_Level|Wearout|Wear_Out|Life_Left|Lifetime_Remain|Life_Remain|Lifetime_Left|Remaining_Life|Remain_Life"
    r"|Percent_Life|Perc_.*Life|Life_Used|Drive_Life|SSD_Life|Endurance",
    re.I,
)

#: what the entries of the ATA error log say went wrong, and whose fault that usually is
ERROR_KINDS = {
    "media": "the drive could not read or find a sector (UNC, IDNF, AMNF): the drive itself",
    "interface": "the data was damaged on the way (ICRC): cable, backplane or controller",
    "aborted": "the drive refused a command (ABRT): usually one it does not support, seldom a fault",
    "other": "something else",
}


def error_kind(description: str) -> str:
    """'Error: ICRC, ABRT at LBA = ...' -> interface."""
    flags = set(re.findall(r"\b[A-Z]{2,5}\b", description.partition(" at ")[0].replace("Error:", "")))
    if flags & {"UNC", "IDNF", "AMNF", "BBK", "MC"}:
        return "media"
    if "ICRC" in flags:
        return "interface"
    if "ABRT" in flags:
        return "aborted"
    return "other"


@dataclass
class NvmeHealth:
    critical_warning: int | None = None
    temperature: int | None = None
    available_spare: int | None = None
    available_spare_threshold: int | None = None
    percentage_used: int | None = None
    data_units_read: int | None = None
    data_units_written: int | None = None
    power_cycles: int | None = None
    power_on_hours: int | None = None
    unsafe_shutdowns: int | None = None
    media_errors: int | None = None
    num_err_log_entries: int | None = None


#: NVMe critical warning bits (NVMe spec, SMART / Health Information log byte 0)
NVME_CRITICAL = {
    0x01: "available spare below threshold",
    0x02: "temperature over or under threshold",
    0x04: "reliability degraded (media or internal error)",
    0x08: "media in read-only mode",
    0x10: "volatile memory backup failed",
}


@dataclass
class SmartReport:
    exit_status: int = 0
    messages: list[str] = field(default_factory=list)
    smartctl_version: str | None = None
    local_time: int | None = None
    device_name: str | None = None
    device_type: str | None = None
    protocol: str | None = None
    model: str | None = None
    model_family: str | None = None
    serial: str | None = None
    wwn: str | None = None
    firmware: str | None = None
    capacity_bytes: int | None = None
    rotation_rate: int | None = None
    form_factor: str | None = None
    in_database: bool | None = None
    passed: bool | None = None
    standby: bool = False
    temp_c: int | None = None
    power_on_hours: int | None = None
    power_cycles: int | None = None
    attrs: list[Attr] = field(default_factory=list)
    ata_error_count: int | None = None
    #: the entries the log still holds (it keeps the last few), as (power-on hour, kind)
    error_entries: list[tuple[int | None, str]] = field(default_factory=list)
    selftest_errors: int | None = None
    selftest_last: str | None = None
    selftest_type: str | None = None
    selftest_hours: int | None = None  # the power-on hour it ran at
    selftest_progress: int | None = None  # percent done of a self-test running now; None when none is
    long_test_minutes: int | None = None  # what the drive says its long self-test takes
    selftests_supported: bool | None = None  # False: an ATA drive that says it runs no self-tests at all
    #: ATA device statistics, "Percentage Used Endurance Indicator": the one wear figure that means the same
    #: on every vendor's SATA SSD, as percentage_used does on NVMe
    endurance_used: int | None = None
    nvme: NvmeHealth | None = None
    scsi_grown_defects: int | None = None
    scsi_uncorrected_errors: int | None = None

    @property
    def identified(self) -> bool:
        return bool(self.serial) and bool(self.model)

    def bit(self, n: int) -> bool:
        return bool(self.exit_status >> n & 1)

    @property
    def exit_flags(self) -> list[str]:
        return [EXIT_BITS[b] for b in range(8) if self.bit(b)]

    @property
    def selftest_age_hours(self) -> int | None:
        """Power-on hours since the last self-test. The log counts the hour in sixteen bits and starts again
        at 65,536: a test at hour 42 on a drive of 65,592 hours ran 14 hours ago, not seven years."""
        if self.selftest_hours is None or not self.power_on_hours:
            return None
        return (self.power_on_hours - self.selftest_hours) % 65536

    @property
    def wear_attrs(self) -> list[Attr]:
        return [a for a in self.attrs if _WEAR_NAME.search(a.name)]

    def recent_errors(self, within_hours: int) -> dict[str, int]:
        """How many of the logged errors, by kind, happened in the last `within_hours` of power-on time."""
        found: dict[str, int] = {}
        if self.power_on_hours is None:
            return found
        for hour, kind in self.error_entries:
            if hour is not None and 0 <= self.power_on_hours - hour <= within_hours:
                found[kind] = found.get(kind, 0) + 1
        return found

    def attr(self, attr_id: int) -> Attr | None:
        for a in self.attrs:
            if a.id == attr_id:
                return a
        return None

    @property
    def model_key(self) -> str:
        return sanitize_key(self.model or "")

    @property
    def serial_key(self) -> str:
        return sanitize_key(self.serial or "")


def _int(v: Any) -> int | None:
    try:
        return None if v is None or isinstance(v, bool) else int(v)
    except (TypeError, ValueError):
        return None


def _get(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def parse_report(doc: dict[str, Any]) -> SmartReport:
    """Normalize one `smartctl -j` document (any device type, any 7.x version)."""
    r = SmartReport()
    sc = doc.get("smartctl") or {}
    r.exit_status = _int(sc.get("exit_status")) or 0
    r.messages = [str(m.get("string", "")) for m in sc.get("messages") or [] if isinstance(m, dict)]
    ver = sc.get("version")
    if isinstance(ver, list) and ver:
        r.smartctl_version = ".".join(str(x) for x in ver)
    r.local_time = _int(_get(doc, "local_time", "time_t"))
    r.device_name = _get(doc, "device", "name")
    r.device_type = _get(doc, "device", "type")
    r.protocol = _get(doc, "device", "protocol")
    r.model = doc.get("model_name") or doc.get("scsi_model_name") or None
    r.model_family = doc.get("model_family")
    r.serial = doc.get("serial_number") or None
    r.wwn = wwn_string(doc.get("wwn"))
    r.firmware = doc.get("firmware_version") or doc.get("scsi_revision")
    r.capacity_bytes = _int(_get(doc, "user_capacity", "bytes"))
    r.rotation_rate = _int(doc.get("rotation_rate"))
    r.form_factor = _get(doc, "form_factor", "name")
    in_db = doc.get("in_smartctl_database")
    r.in_database = in_db if isinstance(in_db, bool) else None
    passed = _get(doc, "smart_status", "passed")
    r.passed = passed if isinstance(passed, bool) else None
    r.temp_c = _int(_get(doc, "temperature", "current"))
    r.power_on_hours = _int(_get(doc, "power_on_time", "hours"))
    r.power_cycles = _int(doc.get("power_cycle_count"))

    for a in _get(doc, "ata_smart_attributes", "table") or []:
        if not isinstance(a, dict) or _int(a.get("id")) is None:
            continue
        raw = a.get("raw") or {}
        flags = a.get("flags") or {}
        r.attrs.append(
            Attr(
                id=int(a["id"]),
                name=str(a.get("name") or f"Attribute_{a['id']}"),
                value=_int(a.get("value")),
                worst=_int(a.get("worst")),
                thresh=_int(a.get("thresh")),
                raw=_int(raw.get("value")) or 0,
                raw_str=str(raw.get("string") or ""),
                when_failed=str(a.get("when_failed") or ""),
                prefail=bool(flags.get("prefailure")),
                updated_online=bool(flags.get("updated_online")),
            )
        )
    # some drives (WD's SATA SSDs) give smartctl a power_on_time of 0 and the hours in attribute 9 all the same
    if not r.power_on_hours:
        poh = r.attr(9)
        if poh is not None:
            hours = leading_int(poh.raw_str) if poh.raw_str else poh.display
            if hours:
                r.power_on_hours = hours
    if r.temp_c is None:
        t = r.attr(194) or r.attr(190)
        if t is not None:
            r.temp_c = t.display

    err = doc.get("ata_smart_error_log") or {}
    r.ata_error_count = _int(_get(err, "extended", "count"))
    if r.ata_error_count is None:
        r.ata_error_count = _int(_get(err, "summary", "count"))
    st = doc.get("ata_smart_self_test_log") or {}
    table = _get(st, "extended", "table") or _get(st, "standard", "table") or []
    r.selftest_errors = _int(_get(st, "extended", "error_count_total"))
    if r.selftest_errors is None:
        r.selftest_errors = _int(_get(st, "standard", "error_count_total"))
    if table and isinstance(table[0], dict):
        status = table[0].get("status") or {}
        r.selftest_last = str(status.get("string") or "") or None
        r.selftest_type = str(_get(table[0], "type", "string") or "") or None
        r.selftest_hours = _int(table[0].get("lifetime_hours"))
    running = _get(doc, "ata_smart_data", "self_test", "status") or {}
    if isinstance(running, dict) and "in progress" in str(running.get("string") or ""):
        left = _int(running.get("remaining_percent"))
        r.selftest_progress = max(0, min(100, 100 - left)) if left is not None else 0
    r.long_test_minutes = _int(_get(doc, "ata_smart_data", "self_test", "polling_minutes", "extended"))
    supported = _get(doc, "ata_smart_data", "capabilities", "self_tests_supported")
    r.selftests_supported = supported if isinstance(supported, bool) else None
    now_testing = _get(doc, "nvme_self_test_log", "current_self_test_operation") or {}
    if isinstance(now_testing, dict) and _int(now_testing.get("value")):
        r.selftest_progress = _int(_get(doc, "nvme_self_test_log", "current_self_test_completion_percent")) or 0
    for entry in _get(err, "extended", "table") or _get(err, "summary", "table") or []:
        if isinstance(entry, dict):
            r.error_entries.append(
                (_int(entry.get("lifetime_hours")), error_kind(str(entry.get("error_description") or "")))
            )
    for stats_page in _get(doc, "ata_device_statistics", "pages") or []:
        for row in (stats_page.get("table") or []) if isinstance(stats_page, dict) else []:
            named = isinstance(row, dict) and row.get("name") == "Percentage Used Endurance Indicator"
            if named and (row.get("flags") or {}).get("valid", True):
                r.endurance_used = _int(row.get("value"))

    n = doc.get("nvme_smart_health_information_log")
    if isinstance(n, dict):
        r.nvme = NvmeHealth(**{k: _int(n.get(k)) for k in NvmeHealth.__dataclass_fields__})
        if r.temp_c is None:
            r.temp_c = r.nvme.temperature
        if r.power_on_hours is None:
            r.power_on_hours = r.nvme.power_on_hours
        if r.power_cycles is None:
            r.power_cycles = r.nvme.power_cycles
    r.scsi_grown_defects = _int(doc.get("scsi_grown_defect_list"))
    ecl = doc.get("scsi_error_counter_log")
    if isinstance(ecl, dict):
        total = 0
        found = False
        for k in ("read", "write", "verify"):
            v = _int(_get(ecl, k, "total_uncorrected_errors"))
            if v is not None:
                total += v
                found = True
        r.scsi_uncorrected_errors = total if found else None

    low_power = any(re.search(r"standby|low-power|sleep mode", m, re.I) for m in r.messages)
    r.standby = (r.exit_status == STANDBY_EXIT and r.passed is None and not r.attrs) or low_power
    return r

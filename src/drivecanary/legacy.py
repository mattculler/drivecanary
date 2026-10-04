"""`smartctl -a` text (smartmontools 6.x and earlier), read into the shape smartctl 7's `--json` has.

For readings taken before `--json` existed: pyvmind kept `smartctl -a` output as text. Only what
`smart.parse_report` reads is produced (identity, the health verdict, the attribute table, the error and
self-test logs), so a capture goes through the same parser and the same verdict as a reading today, and is
filed under the same drive if that drive is ever seen again.

What text cannot say: the packed 48 bits of an attribute's raw value. The number smartctl printed stands in
for it, which is what `display_raw` makes of the packed value for every attribute that is judged or charted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from drivecanary.config import StatusConfig
from drivecanary.ingest import store_run
from drivecanary.models import Drive, DriveSighting, Host, HostState, SampleSource, SmartRun
from drivecanary.smart import parse_report
from drivecanary.timeutil import from_epoch, local_to_utc

_ATTR = re.compile(
    r"^\s*(\d+)\s+(\S+)\s+0x([0-9a-fA-F]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(Pre-fail|Old_age)\s+(Always|Offline)"
    r"\s+(\S+)\s+(.*?)\s*$"
)
_SELFTEST = re.compile(r"^#\s*(\d+)\s+(.+?)\s{2,}(.+?)\s{2,}(\d+)%\s+(\d+)\s+(\S+)")
_ERROR = re.compile(r"^Error (\d+) (?:occurred )?at disk power-on lifetime: (\d+) hours")
_LEADING_INT = re.compile(r"-?\d+")


def _leading_int(s: str) -> int | None:
    m = _LEADING_INT.match(s.strip().replace(",", ""))
    return int(m.group()) if m else None


def _info(text: str) -> dict[str, str]:
    """The `Key: value` lines of the information section."""
    out: dict[str, str] = {}
    section = text.partition("=== START OF INFORMATION SECTION ===")[2].partition("=== START OF")[0]
    for line in section.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out.setdefault(key.strip(), value.strip())
    return out


def _local_time(value: str, tz: ZoneInfo) -> int:
    """'Thu Oct 26 23:17:54 2017 EDT' in `tz`, as epoch seconds. The abbreviation must be `tz`'s own at that
    moment: a capture from a host in another zone needs its zone given."""
    parts = value.split()
    if len(parts) < 5:
        raise ValueError(f"cannot read the capture's time: {value!r}")
    naive = datetime.strptime(" ".join(parts[:5]), "%a %b %d %H:%M:%S %Y")
    when = local_to_utc(naive, tz)
    said = parts[5] if len(parts) > 5 else None
    if said and said != when.astimezone(tz).tzname():
        raise ValueError(f"the capture says {said}, which is not {tz.key} on {naive:%Y-%m-%d}: give its zone (--tz)")
    return int(when.timestamp())


def _wwn(value: str) -> dict[str, int] | None:
    parts = value.split()
    if len(parts) != 3:
        return None
    try:
        return {"naa": int(parts[0], 16), "oui": int(parts[1], 16), "id": int(parts[2], 16)}
    except ValueError:
        return None


def _attributes(text: str) -> list[dict[str, Any]]:
    table = []
    section = text.partition("Vendor Specific SMART Attributes with Thresholds:")[2]
    for line in section.splitlines()[1:]:
        m = _ATTR.match(line)
        if m is None:
            if line.strip():
                continue
            break  # the table ends at its first blank line
        attr_id, name, flags, value, worst, thresh, kind, updated, when_failed, raw = m.groups()
        table.append(
            {
                "id": int(attr_id),
                "name": name,
                "value": int(value),
                "worst": int(worst),
                "thresh": int(thresh),
                "when_failed": "" if when_failed == "-" else when_failed,
                "flags": {
                    "value": int(flags, 16),
                    "prefailure": kind == "Pre-fail",
                    "updated_online": updated == "Always",
                },
                "raw": {"value": _leading_int(raw) or 0, "string": raw},
            }
        )
    return table


def _error_log(text: str) -> dict[str, Any]:
    if "No Errors Logged" in text:
        return {"summary": {"count": 0, "table": []}}
    m = re.search(r"ATA Error Count:\s*(\d+)", text)
    if m is None:
        return {}
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        found = _ERROR.match(line)
        if found:
            entries.append({"error_number": int(found.group(1)), "lifetime_hours": int(found.group(2))})
        elif entries and "error_description" not in entries[-1] and "Error: " in line:
            entries[-1]["error_description"] = line[line.index("Error: ") :].strip()
    return {"summary": {"count": int(m.group(1)), "table": entries}}


def _selftest_log(text: str) -> dict[str, Any]:
    table: list[dict[str, Any]] = []
    errors = 0
    for line in text.partition("Self-test log structure")[2].splitlines():
        m = _SELFTEST.match(line)
        if m is None:
            continue
        _, kind, status, remaining, hours, _lba = m.groups()
        failed = ("fail" in status.lower() or "error" in status.lower()) and "without error" not in status
        errors += failed
        table.append(
            {
                "type": {"string": kind.strip()},
                "status": {"string": status.strip(), "remaining_percent": int(remaining), "passed": not failed},
                "lifetime_hours": int(hours),
            }
        )
    if not table:
        return {}
    return {"standard": {"count": len(table), "error_count_total": errors, "table": table}}


def to_json(text: str, tz: ZoneInfo) -> dict[str, Any]:
    """A `smartctl -a` capture as smartctl 7 would have put it in JSON, as far as drivecanary reads it."""
    info = _info(text)
    if "Device Model" not in info or "Serial Number" not in info:
        raise ValueError("not an ATA `smartctl -a` capture: no Device Model and Serial Number")
    if "Local Time is" not in info:
        raise ValueError("the capture does not say when it was taken (no 'Local Time is' line)")
    version = re.match(r"smartctl (\d+)\.(\d+)", text.lstrip())
    doc: dict[str, Any] = {
        "smartctl": {"version": [int(version.group(1)), int(version.group(2))] if version else None, "exit_status": 0},
        "device": {"protocol": "ATA"},
        "model_name": info["Device Model"],
        "serial_number": info["Serial Number"],
        "local_time": {"time_t": _local_time(info["Local Time is"], tz)},
        "ata_smart_attributes": {"table": _attributes(text)},
        "ata_smart_error_log": _error_log(text),
        "ata_smart_self_test_log": _selftest_log(text),
    }
    if "Model Family" in info:
        doc["model_family"] = info["Model Family"]
    if "Firmware Version" in info:
        doc["firmware_version"] = info["Firmware Version"]
    if wwn := _wwn(info.get("LU WWN Device Id", "")):
        doc["wwn"] = wwn
    if (capacity := _leading_int(info.get("User Capacity", ""))) is not None:
        doc["user_capacity"] = {"bytes": capacity}
    rotation = info.get("Rotation Rate", "")
    if rotation.startswith("Solid State"):
        doc["rotation_rate"] = 0
    elif (rpm := _leading_int(rotation)) is not None:
        doc["rotation_rate"] = rpm
    if "Form Factor" in info:
        doc["form_factor"] = {"name": info["Form Factor"]}
    if "Device is" in info:
        doc["in_smartctl_database"] = info["Device is"].startswith("In smartctl database")
    health = re.search(r"self-assessment test result:\s*(\w+)", text)
    if health:
        doc["smart_status"] = {"passed": health.group(1) == "PASSED"}
    long_test = re.search(r"Extended self-test routine\s*\n\s*recommended polling time:\s*\(\s*(\d+)\)", text)
    if long_test:
        doc["ata_smart_data"] = {"self_test": {"polling_minutes": {"extended": int(long_test.group(1))}}}
    attrs = {a["id"]: a for a in doc["ata_smart_attributes"]["table"]}
    if 9 in attrs:
        doc["power_on_time"] = {"hours": _leading_int(attrs[9]["raw"]["string"])}
    if 12 in attrs:
        doc["power_cycle_count"] = attrs[12]["raw"]["value"]
    for temp_id in (194, 190):
        if temp_id in attrs:
            doc["temperature"] = {"current": attrs[temp_id]["raw"]["value"]}
            break
    return doc


@dataclass
class Imported:
    drive: Drive
    collected_at: datetime
    stored: bool  # False: a reading of that drive at that moment was already there
    new_drive: bool


def retired_host(session: Session, name: str) -> tuple[Host, bool]:
    """The host a capture came from: the one of that name, or a new one, retired, if there is none. A host
    whose history is all that is left is kept so that its drives can say where they were; `host set NAME
    --state pending` and its install bring it back."""
    host = session.scalar(select(Host).where(Host.name == name))
    if host is not None:
        return host, False
    host = Host(name=name, address=name, state=HostState.RETIRED.value)
    session.add(host)
    session.flush()
    return host, True


def import_text(session: Session, text: str, *, tz: ZoneInfo, cfg: StatusConfig, host: Host | None = None) -> Imported:
    """Store one capture as a reading of its drive, on `host` if it is said where it was taken. A drive
    drivecanary has never seen is created retired, so a 2017 reading does not sit on the front page as stale;
    the first live reading of it clears that. Again with a host for a reading already stored: it is filed
    under that host."""
    doc = to_json(text, tz)
    report = parse_report(doc)
    assert report.local_time is not None
    when = from_epoch(report.local_time)
    drive = session.scalar(
        select(Drive).where(Drive.model_key == report.model_key, Drive.serial_key == report.serial_key)
    )
    new_drive = drive is None
    if drive is None:
        drive = Drive(
            model_key=report.model_key,
            serial_key=report.serial_key,
            identity_source="legacy",
            first_seen_at=when,
            last_seen_at=when,
            retired=True,
        )
        session.add(drive)
    # what a newer reading said about the drive stands; the capture only fills gaps
    for field, value in (
        ("model", report.model),
        ("serial", report.serial),
        ("wwn", report.wwn),
        ("protocol", report.protocol),
        ("capacity_bytes", report.capacity_bytes),
        ("rotation_rate", report.rotation_rate),
        ("model_family", report.model_family),
        ("firmware", report.firmware),
        ("form_factor", report.form_factor),
    ):
        if getattr(drive, field) is None and value is not None:
            setattr(drive, field, value)
    if when < drive.first_seen_at:
        drive.first_seen_at = when
    session.flush()
    if host is not None:
        seen = session.scalar(
            select(DriveSighting).where(DriveSighting.drive_id == drive.id, DriveSighting.host_id == host.id)
        )
        if seen is None:
            # the capture does not say which device node it was: it was on this host, then
            session.add(
                DriveSighting(
                    drive_id=drive.id,
                    host_id=host.id,
                    dev_name="",
                    first_seen_at=when,
                    last_seen_at=when,
                    current=False,
                )
            )
    stored = session.scalar(select(SmartRun).where(SmartRun.drive_id == drive.id, SmartRun.collected_at == when))
    if stored is not None:
        if host is not None and stored.host_id is None:
            stored.host_id = host.id
        return Imported(drive, when, stored=False, new_drive=new_drive)
    store_run(
        session,
        drive,
        report,
        source=SampleSource.LEGACY,
        collected_at=when,
        host_id=host.id if host is not None else None,
        attempt_id=None,
        raw=json.dumps(doc).encode(),
        cfg=cfg,
    )
    return Imported(drive, when, stored=True, new_drive=new_drive)

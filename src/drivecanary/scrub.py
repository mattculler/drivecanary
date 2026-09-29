"""What a pool's scrub output says, in one line: did it finish, when, how long did it take, what did it find.

The full text is stored as the host printed it (the pool page shows it whole); this is the summary for a
table cell. Times are the host's local time, as printed: a scrub's finish is its start plus its duration.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_DATE = "%a %b %d %H:%M:%S %Y"
_KV = re.compile(r"^\s*([A-Za-z][A-Za-z ]*?):\s*(.*?)\s*$")
_OLD_BTRFS = re.compile(r"scrub started at (?P<start>.+?) and (?P<what>finished|was aborted) after (?P<took>[\d:]+)")
_OLD_BTRFS_RUNNING = re.compile(r"scrub started at (?P<start>.+?), running for (?P<took>[\d:]+)")
_OLD_BTRFS_ERRORS = re.compile(r"with (\d+) errors")
_ZFS_DONE = re.compile(r"scrub repaired (?P<repaired>\S+) in (?P<took>.+?) with (?P<errors>\d+) errors on (?P<end>.+)$")
_ZFS_RUNNING = re.compile(r"scrub in progress since (?P<start>.+)$")


def _when(text: str) -> datetime | None:
    try:
        return datetime.strptime(" ".join(text.split()), _DATE)
    except ValueError:
        return None


def _span(text: str) -> timedelta | None:
    """'5:12:33', '125:00:01', or ZFS's '0 days 05:12:33'."""
    m = re.match(r"(?:(\d+) days? )?(\d+):(\d\d):(\d\d)$", text.strip())
    if not m:
        return None
    days, h, mi, s = (int(x or 0) for x in m.groups())
    return timedelta(days=days, hours=h, minutes=mi, seconds=s)


def _fmt(dt: datetime) -> str:
    return f"{dt:%Y-%m-%d %H:%M}"


def _finished(start: datetime | None, took: str, found: str) -> str:
    span = _span(took)
    end = _fmt(start + span) if start is not None and span is not None else None
    head = f"finished {end}" if end else "finished"
    return f"{head}, took {took}; {found}" if found else f"{head}, took {took}"


def summarize_btrfs(text: str) -> str:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        m = _KV.match(line)
        if m:
            fields[m.group(1).strip().lower()] = m.group(2)
    if "status" in fields:
        status = fields["status"]
        start = _when(fields.get("scrub started", "") or fields.get("scrub resumed", ""))
        took = fields.get("duration", "")
        found = fields.get("error summary", "")
        if status == "finished":
            return _finished(start, took, found)
        if status == "running":
            done = re.search(r"\(([\d.]+%)\)", fields.get("bytes scrubbed", ""))
            parts = ["running"]
            if start is not None:
                parts.append(f"since {_fmt(start)}")
            if done:
                parts.append(f"{done.group(1)} done")
            if fields.get("time left"):
                parts.append(f"{fields['time left']} left")
            return ", ".join(parts) + (f"; {found}" if found else "")
        since = f" (started {_fmt(start)})" if start is not None else ""
        return f"{status} after {took}{since}" + (f"; {found}" if found else "")
    m = _OLD_BTRFS.search(text)  # btrfs-progs before 5.2
    if m:
        errors = _OLD_BTRFS_ERRORS.search(text)
        found = f"{errors.group(1)} errors" if errors else ""
        if m.group("what") == "finished":
            return _finished(_when(m.group("start")), m.group("took"), found)
        return f"aborted after {m.group('took')}" + (f"; {found}" if found else "")
    m = _OLD_BTRFS_RUNNING.search(text)
    if m:
        return f"running for {m.group('took')}"
    if "no stats available" in text:
        return "never scrubbed"
    return " ".join(text.split())[:90]


def summarize_zfs(text: str) -> str:
    line = " ".join(text.split())
    m = _ZFS_DONE.search(line)
    if m:
        end = _when(m.group("end"))
        head = f"finished {_fmt(end)}" if end is not None else "finished"
        return f"{head}, took {m.group('took')}; {m.group('errors')} errors, repaired {m.group('repaired')}"
    m = _ZFS_RUNNING.search(line)
    if m:
        start = _when(m.group("start"))
        return f"running since {_fmt(start)}" if start is not None else "running"
    if line.startswith("none requested"):
        return "never scrubbed"
    return line[:90]


def running(kind: str, text: str | None) -> str | None:
    """'18.42%' (or '' when it does not say how far) while a scrub, resync or check is under way; else None."""
    if not text:
        return None
    if kind == "btrfs":
        if not re.search(r"^\s*Status:\s*running", text, re.M) and not _OLD_BTRFS_RUNNING.search(text):
            return None
        done = re.search(r"\(([\d.]+%)\)", text)
        return done.group(1) if done else ""
    if kind == "zfs":
        if not re.search(r"(scrub|resilver) in progress", text):
            return None
        done = re.search(r"([\d.]+%) done", text)
        return done.group(1) if done else ""
    done = re.search(r"(?:resync|recovery|reshape|check)\s*=\s*([\d.]+%)", text)
    return done.group(1) if done else None


def summarize(kind: str, text: str | None) -> str:
    if not text:
        return ""
    if kind == "btrfs":
        return summarize_btrfs(text)
    if kind == "zfs":
        return summarize_zfs(text)
    return " ".join(text.split())[:90]


def btrfs_found_errors(text: str | None) -> str | None:
    """The scrub's error summary when it is anything but clean, else None."""
    if not text:
        return None
    for line in text.splitlines():
        m = _KV.match(line)
        if m and m.group(1).strip().lower() == "error summary":
            return None if m.group(2).startswith("no errors") else m.group(2)
    m = _OLD_BTRFS_ERRORS.search(text)
    if m and int(m.group(1)) > 0:
        return f"{m.group(1)} errors"
    return None

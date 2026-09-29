"""When a host's drives test themselves: smartd.conf's `-s` schedules, read and explained.

smartd starts a test in any hour whose `T/MM/DD/d/HH` string (test type, month, day, day of the week, hour)
matches the directive's regular expression. That is the whole of the rule, so the next time a test runs is
found the way smartd finds it: by trying the hours to come against the expression. Where there is no smartd
(OPNsense), the install writes what its cron jobs do in the same words, and this reads that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

TYPES = {"L": "long", "S": "short", "C": "conveyance", "O": "offline"}
#: how far ahead to look: a yearly test is at most a year away
HORIZON_HOURS = 24 * 400
_STAGGER = re.compile(r":\d{3}(-\d{3})?")


@dataclass
class Entry:
    device: str  # a device path, or DEVICESCAN for every device not named
    regex: str | None  # the -s expression, as written
    line: str
    next: dict[str, datetime] = field(default_factory=dict)  # test type -> when next, local to the host
    problem: str | None = None


def _directive(tokens: list[str], flag: str) -> str | None:
    for i, tok in enumerate(tokens[:-1]):
        if tok == flag:
            return tokens[i + 1]
    return None


def parse(text: str | None) -> list[Entry]:
    """The entries of a smartd.conf that smartd would act on: up to and including DEVICESCAN."""
    out: list[Entry] = []
    defaults: str | None = None
    joined = (text or "").replace("\\\n", " ")
    for raw in joined.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        device = tokens[0]
        regex = _directive(tokens, "-s")
        if device == "DEFAULT":
            defaults = regex
            continue
        if _directive(tokens, "-d") == "ignore":
            continue
        out.append(Entry(device=device, regex=regex if regex is not None else defaults, line=line))
        if device == "DEVICESCAN":
            break  # smartd reads no further
    return out


def next_runs(regex: str, now: datetime) -> tuple[dict[str, datetime], str | None]:
    """For each test type the expression schedules, the start of the next hour it matches."""
    try:
        pattern = re.compile(_STAGGER.sub("", regex))
    except re.error as e:
        return {}, f"not a regular expression: {e}"
    found: dict[str, datetime] = {}
    hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(HORIZON_HOURS):
        stamp = f"{hour.month:02d}/{hour.day:02d}/{hour.isoweekday()}/{hour.hour:02d}"
        for letter, name in TYPES.items():
            if name not in found and pattern.fullmatch(f"{letter}/{stamp}"):
                found[name] = hour
        if len(found) == len(TYPES):
            break
        hour += timedelta(hours=1)
    return found, None


def schedule(text: str | None, now: datetime) -> list[Entry]:
    """`now` is the host's local time, naive: smartd matches against the host's own clock."""
    entries = parse(text)
    for e in entries:
        if e.regex:
            e.next, e.problem = next_runs(e.regex, now)
    return entries


def entry_for(entries: list[Entry], dev_name: str, serial_key: str) -> Entry | None:
    """The entry that governs a drive: the one that names it (by its device, or by a /dev/disk/by-id name,
    which has the serial in it), else DEVICESCAN, which is everything not named."""
    squashed_serial = re.sub(r"[^A-Za-z0-9]", "_", serial_key)
    for e in entries:
        if e.device == "DEVICESCAN":
            continue
        name = e.device.rsplit("/", 1)[-1]
        if e.device == dev_name or name == dev_name.rsplit("/", 1)[-1]:
            return e
        if len(squashed_serial) >= 6 and squashed_serial in re.sub(r"[^A-Za-z0-9]", "_", name):
            return e
    return next((e for e in entries if e.device == "DEVICESCAN"), None)


def is_scheduled(text: str | None, dev_name: str, serial_key: str) -> bool:
    e = entry_for(parse(text), dev_name, serial_key)
    return e is not None and bool(e.regex)

"""Time helpers. Everything stored is UTC; the only local times in the system are the ones smartd wrote."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utcnow() -> datetime:
    return datetime.now(UTC)


def from_epoch(seconds: int | float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def to_epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return int(dt.timestamp())


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown time zone {name!r} (an Olson name such as America/New_York)") from e


def local_to_utc(naive: datetime, tz: ZoneInfo, after: datetime | None = None) -> datetime:
    """Resolve a naive wall-clock time to UTC.

    In the repeated hour of a DST fall-back the wall clock names two instants an hour apart. `after`, the
    previous sample's UTC time, picks the one that keeps time moving forward; without it the first is taken.
    A clock that really went backwards (atlas's did, by 7 h, on 2024-03-08) is stored as it was read.
    """
    if naive.tzinfo is not None:
        raise ValueError("aware datetime given where a wall-clock reading was expected")
    first = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
    second = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    if first != second and after is not None and first <= after < second:
        return second
    return first


def spelled(hours: float | None) -> str:
    """A count of hours as a person would put it: 25 days, 3 months, 1.3 years. Nothing under two days,
    where the hours already say it."""
    if hours is None or hours < 48:
        return ""
    days = hours / 24
    if days < 60:
        return f"{days:.0f} days"
    if days < 2 * 365.25:
        months = days / 30.44
        return f"{months:.0f} month{'s' if round(months) != 1 else ''}"
    return f"{days / 365.25:.1f} years"


def with_span(hours: int | None, unit: str = "h") -> str:
    """`9,070 h (1.0 years)`: the count, and the span in parentheses where that says more."""
    if hours is None:
        return ""
    said = spelled(hours)
    count = f"{hours:,} {unit}".rstrip()
    return f"{count} ({said})" if said else count


def minutes_taking(minutes: int | None) -> str:
    """How long something takes: 13 minutes, 1.4 hours, 1.5 days."""
    if minutes is None:
        return ""
    if minutes < 120:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


def shown(dt: datetime | None, zone_name: str, *, seconds: bool = False) -> str:
    """A stored (UTC) time as it is shown: in the zone the page is set to, with the zone's own abbreviation,
    which says whether daylight saving was on."""
    if dt is None:
        return ""
    return dt.astimezone(zone(zone_name)).strftime("%Y-%m-%d %H:%M:%S %Z" if seconds else "%Y-%m-%d %H:%M %Z")


def hours_ago(dt: datetime | None, now: datetime | None = None) -> float | None:
    if dt is None:
        return None
    return ((now or utcnow()) - dt).total_seconds() / 3600

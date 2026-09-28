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


def hours_ago(dt: datetime | None, now: datetime | None = None) -> float | None:
    if dt is None:
        return None
    return ((now or utcnow()) - dt).total_seconds() / 3600

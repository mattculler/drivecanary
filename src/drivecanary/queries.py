"""Read-side queries shared by the page and the terminal: the latest word on every drive, host and
pool, and whether it is too old to trust."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from drivecanary.config import Config
from drivecanary.models import (
    AttrSample,
    CollectionRun,
    Drive,
    DriveSighting,
    Host,
    HostAttempt,
    HostState,
    Pool,
    PoolStatus,
    SmartRun,
    Verdict,
)
from drivecanary.smart import display_raw
from drivecanary.timeutil import hours_ago, utcnow

#: worst first on every list
DISPLAY_ORDER = {
    Verdict.FAIL: 0,
    Verdict.WARN: 1,
    Verdict.ERROR: 2,
    Verdict.STALE: 3,
    Verdict.UNKNOWN: 4,
    Verdict.SKIPPED: 5,
    Verdict.OK: 6,
}


def rank(v: Verdict | str) -> int:
    return DISPLAY_ORDER.get(Verdict(v), 9)


@dataclass
class DriveRow:
    drive: Drive
    host: Host | None
    dev_name: str | None
    latest: SmartRun | None
    verdict: Verdict
    reasons: list[str]
    age_hours: float | None
    counters: dict[int, int] = field(default_factory=dict)  # attr id -> display raw (ATA)

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (rank(self.verdict), self.host.name if self.host else "~", self.dev_name or "")


@dataclass
class HostRow:
    host: Host
    verdict: Verdict
    age_hours: float | None
    drives: int
    last_attempt: HostAttempt | None


@dataclass
class PoolRow:
    pool: Pool
    host: Host
    latest: PoolStatus | None
    verdict: Verdict
    age_hours: float | None


@dataclass
class Overview:
    now: datetime
    drives: list[DriveRow]
    hosts: list[HostRow]
    pools: list[PoolRow]
    last_run: CollectionRun | None
    verdict: Verdict
    counts: dict[str, int]


def latest_run(session: Session, drive_id: int) -> SmartRun | None:
    return session.scalar(
        select(SmartRun).where(SmartRun.drive_id == drive_id).order_by(SmartRun.collected_at.desc()).limit(1)
    )


def current_sighting(session: Session, drive_id: int) -> DriveSighting | None:
    return session.scalar(
        select(DriveSighting)
        .where(DriveSighting.drive_id == drive_id)
        .order_by(DriveSighting.current.desc(), DriveSighting.last_seen_at.desc())
        .limit(1)
    )


def run_counters(session: Session, run: SmartRun, attr_ids: list[int]) -> dict[int, int]:
    rows = session.execute(
        select(AttrSample.attr_id, AttrSample.raw).where(AttrSample.run_id == run.id, AttrSample.attr_id.in_(attr_ids))
    )
    return {attr_id: display_raw(attr_id, raw) for attr_id, raw in rows}


def _staled(verdict: Verdict, age: float | None, cfg: Config) -> Verdict:
    if age is None:
        return Verdict.UNKNOWN
    if age > cfg.collect.stale_after_hours and verdict not in (Verdict.FAIL,):
        return Verdict.STALE
    return verdict


def drive_rows(session: Session, cfg: Config, now: datetime) -> list[DriveRow]:
    rows: list[DriveRow] = []
    for drive in session.scalars(select(Drive).where(~Drive.retired)):
        s = current_sighting(session, drive.id)
        latest = latest_run(session, drive.id)
        age = hours_ago(latest.collected_at, now) if latest else None
        verdict = Verdict(latest.verdict) if latest else Verdict.UNKNOWN
        reasons = latest.reasons.splitlines() if latest and latest.reasons else []
        counters = run_counters(session, latest, cfg.status.ata_warn_attributes) if latest else {}
        rows.append(
            DriveRow(
                drive=drive,
                host=s.host if s else None,
                dev_name=s.dev_name if s else None,
                latest=latest,
                verdict=_staled(verdict, age, cfg),
                reasons=reasons,
                age_hours=age,
                counters=counters,
            )
        )
    rows.sort(key=lambda r: r.sort_key)
    return rows


def last_attempt(session: Session, host_id: int) -> HostAttempt | None:
    return session.scalar(
        select(HostAttempt).where(HostAttempt.host_id == host_id).order_by(HostAttempt.started_at.desc()).limit(1)
    )


def host_rows(session: Session, cfg: Config, now: datetime, drives: list[DriveRow]) -> list[HostRow]:
    out: list[HostRow] = []
    for host in session.scalars(select(Host).order_by(Host.name)):
        mine = [d for d in drives if d.host is not None and d.host.id == host.id]
        age = hours_ago(host.last_success_at, now)
        if host.state in (HostState.PAUSED.value, HostState.RETIRED.value):
            verdict = Verdict.SKIPPED
        elif host.state == HostState.PENDING.value:
            verdict = Verdict.UNKNOWN
        elif host.state in (HostState.UNREACHABLE.value, HostState.BROKEN.value):
            verdict = Verdict.ERROR
        elif age is not None and age > cfg.collect.stale_after_hours:
            verdict = Verdict.STALE
        else:
            verdict = Verdict.OK
        out.append(
            HostRow(
                host=host, verdict=verdict, age_hours=age, drives=len(mine), last_attempt=last_attempt(session, host.id)
            )
        )
    out.sort(key=lambda r: (rank(r.verdict), r.host.name))
    return out


def latest_pool_status(session: Session, pool_id: int) -> PoolStatus | None:
    return session.scalar(
        select(PoolStatus).where(PoolStatus.pool_id == pool_id).order_by(PoolStatus.collected_at.desc()).limit(1)
    )


def pool_rows(session: Session, cfg: Config, now: datetime) -> list[PoolRow]:
    out: list[PoolRow] = []
    for pool, host in session.execute(select(Pool, Host).join(Host, Pool.host_id == Host.id).where(~Pool.retired)):
        latest = latest_pool_status(session, pool.id)
        age = hours_ago(latest.collected_at, now) if latest else None
        verdict = Verdict(latest.verdict) if latest else Verdict.UNKNOWN
        out.append(PoolRow(pool=pool, host=host, latest=latest, verdict=_staled(verdict, age, cfg), age_hours=age))
    out.sort(key=lambda r: (rank(r.verdict), r.host.name, r.pool.name))
    return out


def overview(session: Session, cfg: Config, now: datetime | None = None) -> Overview:
    now = now or utcnow()
    drives = drive_rows(session, cfg, now)
    hosts = host_rows(session, cfg, now, drives)
    pools = pool_rows(session, cfg, now)
    last = session.scalar(select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(1))
    verdicts = [d.verdict for d in drives] + [p.verdict for p in pools] + [h.verdict for h in hosts]
    overall = min(verdicts, key=rank) if verdicts else Verdict.UNKNOWN
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v.value] = counts.get(v.value, 0) + 1
    return Overview(now=now, drives=drives, hosts=hosts, pools=pools, last_run=last, verdict=overall, counts=counts)


# --------------------------------------------------------------------------- one drive


def drive_runs(session: Session, drive_id: int, limit: int = 40) -> list[SmartRun]:
    return list(
        session.scalars(
            select(SmartRun).where(SmartRun.drive_id == drive_id).order_by(SmartRun.collected_at.desc()).limit(limit)
        )
    )


def run_attrs(session: Session, run: SmartRun) -> list[AttrSample]:
    return list(session.scalars(select(AttrSample).where(AttrSample.run_id == run.id).order_by(AttrSample.attr_id)))


def drive_sightings(session: Session, drive_id: int) -> list[DriveSighting]:
    return list(
        session.scalars(
            select(DriveSighting).where(DriveSighting.drive_id == drive_id).order_by(DriveSighting.last_seen_at.desc())
        )
    )


def sample_span(session: Session, drive_id: int) -> tuple[datetime | None, datetime | None, int]:
    row = session.execute(
        select(func.min(SmartRun.collected_at), func.max(SmartRun.collected_at), func.count(SmartRun.id)).where(
            SmartRun.drive_id == drive_id
        )
    ).one()
    lo, hi, n = row  # min()/max() of an Epoch column come back through the Epoch type already
    return (lo, hi, int(n or 0))


#: series the chart can ask for: metric key -> (label, unit)
RUN_METRICS: dict[str, tuple[str, str]] = {
    "temp": ("Temperature", "°C"),
    "poh": ("Power-on hours", "h"),
    "nvme_percentage_used": ("Endurance used", "%"),
    "nvme_available_spare": ("Available spare", "%"),
    "nvme_media_errors": ("Media errors", ""),
    "nvme_err_log_entries": ("Error log entries", ""),
    "nvme_unsafe_shutdowns": ("Unsafe shutdowns", ""),
    "scsi_grown_defects": ("Grown defects", ""),
}
_RUN_COLUMNS = {
    "temp": SmartRun.temp_c,
    "poh": SmartRun.power_on_hours,
    "nvme_percentage_used": SmartRun.nvme_percentage_used,
    "nvme_available_spare": SmartRun.nvme_available_spare,
    "nvme_media_errors": SmartRun.nvme_media_errors,
    "nvme_err_log_entries": SmartRun.nvme_err_log_entries,
    "nvme_unsafe_shutdowns": SmartRun.nvme_unsafe_shutdowns,
    "scsi_grown_defects": SmartRun.scsi_grown_defects,
}


def downsample(points: list[tuple[int, float]], max_points: int) -> list[tuple[int, float]]:
    """Bucket-average a series down to at most max_points; the first timestamp of each bucket stands for it."""
    if len(points) <= max_points:
        return points
    size = math.ceil(len(points) / max_points)
    out: list[tuple[int, float]] = []
    for i in range(0, len(points), size):
        chunk = points[i : i + size]
        out.append((chunk[0][0], sum(p[1] for p in chunk) / len(chunk)))
    return out


def series(
    session: Session, drive_id: int, metric: str, since: datetime | None, max_points: int
) -> list[tuple[int, float]]:
    """[(epoch, value), ...] for one metric of one drive: `attr:<id>` (display raw), `attr:<id>:value`
    (the normalized value), or one of RUN_METRICS."""
    pts: list[tuple[int, float]] = []
    if metric.startswith("attr:"):
        parts = metric.split(":")
        attr_id = int(parts[1])
        want_value = len(parts) > 2 and parts[2] == "value"
        qa = select(AttrSample.collected_at, AttrSample.raw, AttrSample.value).where(
            AttrSample.drive_id == drive_id, AttrSample.attr_id == attr_id
        )
        if since is not None:
            qa = qa.where(AttrSample.collected_at >= since)
        for ts, raw, value in session.execute(qa.order_by(AttrSample.collected_at)):
            y = value if want_value else display_raw(attr_id, raw)
            if y is not None:
                pts.append((int(ts.timestamp()), float(y)))
    elif metric in _RUN_COLUMNS:
        col = _RUN_COLUMNS[metric]
        qr = select(SmartRun.collected_at, col).where(SmartRun.drive_id == drive_id, col.is_not(None))
        if since is not None:
            qr = qr.where(SmartRun.collected_at >= since)
        for ts, y in session.execute(qr.order_by(SmartRun.collected_at)):
            if y is not None:
                pts.append((int(ts.timestamp()), float(y)))
    else:
        raise KeyError(metric)
    return downsample(pts, max_points)


def since_for(window: str, now: datetime) -> datetime | None:
    """'7d', '30d', '1y', 'all' -> the cutoff."""
    units = {"d": 1, "w": 7, "m": 30, "y": 365}
    if window in ("all", ""):
        return None
    try:
        n, u = int(window[:-1]), window[-1]
        return now - timedelta(days=n * units[u])
    except (ValueError, KeyError):
        return now - timedelta(days=30)

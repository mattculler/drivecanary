"""Read-side queries shared by the page and the terminal: the latest word on every drive, host and
pool, and whether it is too old to trust."""

from __future__ import annotations

import json
import math
import zlib
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
from drivecanary.scrub import running as scrub_running
from drivecanary.smart import SmartReport, display_raw, parse_report
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
    testing: int | None = None  # percent done of a self-test running now

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
    scrubbing: str | None = None  # '18.42%', or '' when it does not say how far; None: no scrub is running


@dataclass
class Overview:
    now: datetime
    drives: list[DriveRow]
    hosts: list[HostRow]
    pools: list[PoolRow]
    last_run: CollectionRun | None  # the ssh collector's; push hosts are in no run
    verdict: Verdict
    counts: dict[str, int]
    hosts_watched: int = 0  # every host that is neither paused nor retired, however it is monitored
    hosts_ok: int = 0
    last_heard: datetime | None = None  # the newest word from any host

    @property
    def testing(self) -> int:
        return sum(1 for d in self.drives if d.testing is not None)

    @property
    def scrubbing(self) -> int:
        return sum(1 for p in self.pools if p.scrubbing is not None)


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


def running_selftest(session: Session, drive_id: int, cfg: Config, now: datetime) -> int | None:
    """How far along a self-test is, by the last thing smartctl said, if it said it lately. An attrlog line
    is newer as often as not and knows nothing of tests; this looks past those."""
    row = session.execute(
        select(SmartRun.selftest_progress, SmartRun.collected_at)
        .where(SmartRun.drive_id == drive_id, SmartRun.source == "smartctl")
        .order_by(SmartRun.collected_at.desc())
        .limit(1)
    ).first()
    if row is None or row[0] is None:
        return None
    age = hours_ago(row[1], now)
    return int(row[0]) if age is not None and age <= cfg.collect.stale_after_hours else None


def drive_rows(session: Session, cfg: Config, now: datetime) -> list[DriveRow]:
    rows: list[DriveRow] = []
    for drive in session.scalars(select(Drive).where(~Drive.retired)):
        s = current_sighting(session, drive.id)
        latest = latest_run(session, drive.id)
        age = hours_ago(latest.collected_at, now) if latest else None
        verdict = Verdict(latest.verdict) if latest else Verdict.UNKNOWN
        reasons = latest.reasons.splitlines() if latest and latest.reasons else []
        counters = run_counters(session, latest, cfg.status.ata_warn_attributes) if latest else {}
        testing = running_selftest(session, drive.id, cfg, now)
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
                testing=testing,
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
            verdict = Verdict.STALE  # pull: the collector has stopped; push: the host has gone quiet
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
        fresh = age is not None and age <= cfg.collect.stale_after_hours
        scrubbing = scrub_running(pool.kind, latest.scrub) if latest is not None and fresh else None
        out.append(
            PoolRow(
                pool=pool,
                host=host,
                latest=latest,
                verdict=_staled(verdict, age, cfg),
                age_hours=age,
                scrubbing=scrubbing,
            )
        )
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
    watched = [h for h in hosts if h.host.active]
    heard = [h.host.last_success_at for h in watched if h.host.last_success_at is not None]
    return Overview(
        now=now,
        drives=drives,
        hosts=hosts,
        pools=pools,
        last_run=last,
        verdict=overall,
        counts=counts,
        hosts_watched=len(watched),
        hosts_ok=sum(1 for h in watched if h.verdict == Verdict.OK),
        last_heard=max(heard) if heard else None,
    )


# --------------------------------------------------------------------------- one drive


def drive_runs(session: Session, drive_id: int, limit: int = 40) -> list[SmartRun]:
    return list(
        session.scalars(
            select(SmartRun).where(SmartRun.drive_id == drive_id).order_by(SmartRun.collected_at.desc()).limit(limit)
        )
    )


def run_attrs(session: Session, run: SmartRun) -> list[AttrSample]:
    return list(session.scalars(select(AttrSample).where(AttrSample.run_id == run.id).order_by(AttrSample.attr_id)))


def latest_report(session: Session, drive_id: int) -> SmartReport | None:
    """What smartctl last said about a drive, whole: the newest reading that still has its JSON. An attrlog
    line is a reading too, and has no names, no logs and no JSON; this looks past those."""
    raw = session.scalar(
        select(SmartRun.raw_json)
        .where(SmartRun.drive_id == drive_id, SmartRun.raw_json.is_not(None))
        .order_by(SmartRun.collected_at.desc())
        .limit(1)
    )
    if raw is None:
        return None
    try:
        doc = json.loads(zlib.decompress(raw))
    except (ValueError, zlib.error):
        return None
    return parse_report(doc) if isinstance(doc, dict) else None


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
    "endurance_used": ("Endurance used", "%"),
    "ata_errors": ("Error log entries", ""),
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
    "endurance_used": SmartRun.endurance_used,
    "ata_errors": SmartRun.ata_error_count,
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

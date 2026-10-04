"""One envelope in, rows out: drives and where they are, one smart_run per device, the pools, and the
attrlog chunks past each cursor. Runs inside the collector's per-host transaction; raises IngestError
with a failure class when the host got the hub nothing it can store."""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from drivecanary import attrlog
from drivecanary.config import Config, StatusConfig
from drivecanary.envelope import Envelope, Frame
from drivecanary.logging import get_logger
from drivecanary.models import (
    AttrlogCursor,
    AttrSample,
    Drive,
    DriveSighting,
    FailureClass,
    Host,
    HostAttempt,
    Pool,
    PoolKind,
    PoolStatus,
    SampleSource,
    SmartRun,
    Verdict,
)
from drivecanary.scrub import btrfs_found_errors
from drivecanary.selftests import is_scheduled
from drivecanary.smart import SmartReport, parse_report
from drivecanary.status import judge_report
from drivecanary.timeutil import from_epoch, utcnow, zone

log = get_logger(__name__)


class IngestError(Exception):
    def __init__(self, failure_class: FailureClass, reason: str) -> None:
        super().__init__(reason)
        self.failure_class = failure_class
        self.reason = reason


@dataclass
class IngestResult:
    drives: int = 0
    runs: int = 0
    pools: int = 0
    attrlog_lines: int = 0
    warnings: list[str] = field(default_factory=list)


def _first_line(f: Frame | None) -> str | None:
    if f is None or not f.out:
        return None
    return f.text.splitlines()[0].strip() if f.text.strip() else None


def _host_zone(host: Host, env: Envelope, cfg: Config, warnings: list[str]) -> ZoneInfo:
    """The zone a host's local timestamps are in: the row's, else what the host reports, else the default."""
    for name in (host.tz, env.header.get("tz")):
        if not name:
            continue
        try:
            return zone(name)
        except ValueError as e:
            warnings.append(f"{e}; falling back")
    return zone(cfg.collect.default_tz)


def ingest_envelope(session: Session, *, host: Host, attempt: HostAttempt, env: Envelope, cfg: Config) -> IngestResult:
    res = IngestResult()
    h = env.header
    attempt.payload_id = h.get("payload_id") or None
    attempt.host_tz = h.get("tz") or None
    if (h.get("collected_at") or "").isdigit():
        attempt.host_collected_at = from_epoch(int(h["collected_at"]))
    if h.get("gate_version", "").isdigit():
        host.gate_version = int(h["gate_version"])
    machine_id = h.get("machine_id") or None
    if machine_id:
        if host.machine_id and host.machine_id != machine_id:
            res.warnings.append(f"machine-id changed from {host.machine_id} to {machine_id}: reinstalled?")
        host.machine_id = machine_id

    probe_exit = env.frame("probe.exit")
    if not env.probe_ran:
        err = (probe_exit.err_text.strip() if probe_exit else "") or "the probe wrote no PROBE-END line"
        if probe_exit is not None and re.search(r"sudo|password|not allowed", err, re.I):
            raise IngestError(FailureClass.SUDO, err)
        rc = probe_exit.rc if probe_exit else None
        raise IngestError(FailureClass.PROBE, f"probe did not finish (rc {rc}): {err[:300]}")
    if not env.complete:
        raise IngestError(FailureClass.ENVELOPE, "envelope truncated: no END line")
    host.probe_version = env.probe_version

    ver = _first_line(env.frame("smartctl.version"))
    if ver:
        m = re.search(r"smartctl (\S+)", ver)
        host.smartctl_version = m.group(1) if m else ver[:64]
    schedule = env.frame("smartd.conf")
    if schedule is not None and schedule.rc == 0:
        host.smartd_conf = schedule.text
    runs_it = _first_line(env.frame("smartd.active"))
    if runs_it:
        host.smartd_state = runs_it[:32]
    lsblk = env.frame("lsblk")
    if lsblk is not None and lsblk.rc == 0 and lsblk.out:
        host.block_devices = lsblk.text
    osr = env.frame("os.release")
    if osr is not None:
        m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', osr.text, re.M)
        if m:
            host.os_release = m.group(1)[:128]

    when_default = attempt.host_collected_at or utcnow()
    seen_dev_names: set[str] = set()
    for f in env.prefixed("smartctl.dev:"):
        _, _, rest = f.name.partition(":")
        dev_name, _, dev_type = rest.partition(":")
        seen_dev_names.add(dev_name)
        try:
            doc = json.loads(f.out or b"{}")
        except ValueError:
            res.warnings.append(f"{dev_name}: smartctl output is not JSON (rc {f.rc})")
            continue
        report = parse_report(doc if isinstance(doc, dict) else {})
        if not report.device_type:
            report.device_type = dev_type or None
        stored = _store_report(session, host, attempt, dev_name, report, f.out, when_default, cfg, res)
        if stored:
            res.runs += 1

    # a sighting not in this collection is no longer current for this host
    for s in session.scalars(select(DriveSighting).where(DriveSighting.host_id == host.id, DriveSighting.current)):
        if s.dev_name not in seen_dev_names:
            s.current = False

    res.pools += _ingest_pools(session, host, attempt, env, when_default, res)
    tz = _host_zone(host, env, cfg, res.warnings)
    for f in env.prefixed("attrlog:"):
        res.attrlog_lines += _ingest_attrlog_chunk(session, host, f, tz, cfg, res)
    return res


# --------------------------------------------------------------------------- drives


def _find_drive(session: Session, report: SmartReport) -> Drive | None:
    if report.identified:
        return session.scalar(
            select(Drive).where(Drive.model_key == report.model_key, Drive.serial_key == report.serial_key)
        )
    return None


def _store_report(
    session: Session,
    host: Host,
    attempt: HostAttempt,
    dev_name: str,
    report: SmartReport,
    raw: bytes,
    when_default: datetime,
    cfg: Config,
    res: IngestResult,
) -> bool:
    drive = _find_drive(session, report)
    now = utcnow()
    if drive is None and report.identified:
        drive = Drive(model_key=report.model_key, serial_key=report.serial_key, first_seen_at=now)
        session.add(drive)
        res.drives += 1
        log.info("drive.new", host=host.name, dev=dev_name, model=report.model, serial=report.serial)
    if drive is None:
        # no identity: a failed open, a drive gone silent. Charge it to whatever was last seen at this node.
        sighting = session.scalar(
            select(DriveSighting)
            .where(DriveSighting.host_id == host.id, DriveSighting.dev_name == dev_name, DriveSighting.current)
            .order_by(DriveSighting.last_seen_at.desc())
        )
        if sighting is None:
            why = "; ".join(report.messages) or f"exit {report.exit_status}"
            res.warnings.append(f"{dev_name}: no identity and no drive known at that node ({why})")
            return False
        drive = sighting.drive
    else:
        drive.model = report.model or drive.model
        drive.serial = report.serial or drive.serial
        drive.wwn = report.wwn or drive.wwn
        drive.protocol = report.protocol or drive.protocol
        drive.device_type = report.device_type or drive.device_type
        drive.rotation_rate = report.rotation_rate if report.rotation_rate is not None else drive.rotation_rate
        drive.capacity_bytes = report.capacity_bytes or drive.capacity_bytes
        drive.model_family = report.model_family or drive.model_family
        drive.firmware = report.firmware or drive.firmware
        drive.form_factor = report.form_factor or drive.form_factor
        drive.identity_source = "smartctl"
        drive.retired = False
    drive.last_seen_at = now
    session.flush()

    sighting = session.scalar(
        select(DriveSighting).where(
            DriveSighting.drive_id == drive.id, DriveSighting.host_id == host.id, DriveSighting.dev_name == dev_name
        )
    )
    if sighting is None:
        sighting = DriveSighting(drive_id=drive.id, host_id=host.id, dev_name=dev_name, first_seen_at=now)
        session.add(sighting)
    sighting.last_seen_at = now
    sighting.current = True

    collected_at = from_epoch(report.local_time) if report.local_time else when_default
    dup = session.scalar(select(SmartRun).where(SmartRun.drive_id == drive.id, SmartRun.collected_at == collected_at))
    if dup is not None and dup.source == SampleSource.SMARTCTL.value:
        res.warnings.append(f"{dev_name}: a reading at {collected_at:%Y-%m-%d %H:%M:%S} is already stored")
        return False
    if dup is not None:
        # an attrlog line of the same second: the reading says more, and attr_sample has room for one of them
        session.execute(delete(AttrSample).where(AttrSample.run_id == dup.id))
        session.delete(dup)
        session.flush()
    scheduled = is_scheduled(host.smartd_conf, dev_name, drive.serial_key)
    store_run(
        session,
        drive,
        report,
        source=SampleSource.SMARTCTL,
        collected_at=collected_at,
        host_id=host.id,
        attempt_id=attempt.id,
        raw=raw,
        cfg=cfg.status,
        scheduled=scheduled,
    )
    return True


def store_run(
    session: Session,
    drive: Drive,
    report: SmartReport,
    *,
    source: SampleSource,
    collected_at: datetime,
    host_id: int | None,
    attempt_id: int | None,
    raw: bytes,
    cfg: StatusConfig,
    scheduled: bool = False,
) -> SmartRun:
    """One reading, judged and stored with its attributes broken out for the trends."""
    verdict, reasons = judge_report(report, cfg, scheduled=scheduled)
    run = SmartRun(
        drive_id=drive.id,
        host_id=host_id,
        attempt_id=attempt_id,
        source=source.value,
        collected_at=collected_at,
        exit_status=report.exit_status,
        passed=report.passed,
        standby=report.standby,
        temp_c=report.temp_c,
        power_on_hours=report.power_on_hours,
        power_cycles=report.power_cycles,
        ata_error_count=report.ata_error_count,
        selftest_errors=report.selftest_errors,
        selftest_last=report.selftest_last,
        selftest_hours=report.selftest_hours,
        selftest_progress=report.selftest_progress,
        endurance_used=report.endurance_used,
        scsi_grown_defects=report.scsi_grown_defects,
        scsi_uncorrected_errors=report.scsi_uncorrected_errors,
        messages=json.dumps(report.messages) if report.messages else None,
        raw_json=zlib.compress(raw) if raw else None,
        verdict=verdict.value,
        reasons="\n".join(reasons) or None,
    )
    if report.nvme is not None:
        n = report.nvme
        run.nvme_percentage_used = n.percentage_used
        run.nvme_available_spare = n.available_spare
        run.nvme_spare_threshold = n.available_spare_threshold
        run.nvme_media_errors = n.media_errors
        run.nvme_critical_warning = n.critical_warning
        run.nvme_err_log_entries = n.num_err_log_entries
        run.nvme_unsafe_shutdowns = n.unsafe_shutdowns
        run.nvme_data_units_written = n.data_units_written
        run.nvme_data_units_read = n.data_units_read
    session.add(run)
    session.flush()
    for a in report.attrs:
        session.add(
            AttrSample(
                drive_id=drive.id,
                attr_id=a.id,
                collected_at=collected_at,
                run_id=run.id,
                value=a.value,
                worst=a.worst,
                thresh=a.thresh,
                raw=a.raw,
                raw_str=a.raw_str[:64] or None,
                when_failed=a.when_failed or None,
                prefail=a.prefail,
            )
        )
    return run


# --------------------------------------------------------------------------- pools


def _pool(session: Session, host: Host, kind: PoolKind, name: str, res: IngestResult) -> Pool:
    pool = session.scalar(select(Pool).where(Pool.host_id == host.id, Pool.kind == kind.value, Pool.name == name))
    if pool is None:
        pool = Pool(host_id=host.id, kind=kind.value, name=name)
        session.add(pool)
        session.flush()
        res.pools += 0
    pool.last_seen_at = utcnow()
    pool.retired = False
    return pool


def _pool_status(
    session: Session,
    pool: Pool,
    attempt: HostAttempt,
    when: datetime,
    *,
    health: str | None,
    verdict: Verdict,
    reasons: list[str],
    raw: str | None,
    read_errors: int | None = None,
    write_errors: int | None = None,
    cksum_errors: int | None = None,
    scrub: str | None = None,
) -> None:
    dup = session.scalar(select(PoolStatus.id).where(PoolStatus.pool_id == pool.id, PoolStatus.collected_at == when))
    if dup is not None:
        return
    session.add(
        PoolStatus(
            pool_id=pool.id,
            attempt_id=attempt.id,
            collected_at=when,
            health=health,
            verdict=verdict.value,
            reasons="\n".join(reasons) or None,
            raw=raw,
            read_errors=read_errors,
            write_errors=write_errors,
            cksum_errors=cksum_errors,
            scrub=scrub,
        )
    )


_ZPOOL_BAD = {"DEGRADED", "FAULTED", "UNAVAIL", "REMOVED", "SUSPENDED"}


def parse_zpool_status(text: str) -> dict[str, dict[str, Any]]:
    """`zpool status -p` text, per pool: state, scan line, errors line, and the summed READ/WRITE/CKSUM columns."""
    pools: dict[str, dict[str, Any]] = {}
    cur: dict[str, Any] | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("pool:"):
            cur = {"state": None, "scan": None, "errors": None, "read": 0, "write": 0, "cksum": 0, "vdev_lines": 0}
            pools[s[5:].strip()] = cur
        elif cur is None:
            continue
        elif s.startswith("state:"):
            cur["state"] = s[6:].strip()
        elif s.startswith("scan:"):
            cur["scan"] = s[5:].strip()
        elif s.startswith("errors:"):
            cur["errors"] = s[7:].strip()
        else:
            m = re.match(r"^(\S+)\s+(ONLINE|DEGRADED|FAULTED|UNAVAIL|REMOVED|OFFLINE)\s+(\d+)\s+(\d+)\s+(\d+)", s)
            if m and m.group(1) != "NAME":
                cur["read"] += int(m.group(3))
                cur["write"] += int(m.group(4))
                cur["cksum"] += int(m.group(5))
                cur["vdev_lines"] += 1
    return pools


def _ingest_zfs(
    session: Session, host: Host, attempt: HostAttempt, env: Envelope, when: datetime, res: IngestResult
) -> int:
    lst = env.frame("zpool.list")
    if lst is None or lst.rc != 0:
        return 0
    status = env.frame("zpool.status")
    detail = parse_zpool_status(status.text) if status is not None and status.rc == 0 else {}
    n = 0
    for line in lst.text.splitlines():
        cols = line.split("\t") if "\t" in line else line.split()
        if len(cols) < 2:
            continue
        name, health = cols[0], cols[1]
        d = detail.get(name, {})
        reasons: list[str] = []
        verdict = Verdict.OK
        if health in _ZPOOL_BAD:
            verdict = Verdict.FAIL
            reasons.append(f"pool {health}")
        elif health != "ONLINE":
            verdict = Verdict.WARN
            reasons.append(f"pool {health}")
        errs = (d.get("read") or 0) + (d.get("write") or 0) + (d.get("cksum") or 0)
        if errs:
            if verdict == Verdict.OK:
                verdict = Verdict.WARN
            reasons.append(f"{d.get('read')} read / {d.get('write')} write / {d.get('cksum')} checksum errors")
        if d.get("errors") and not str(d["errors"]).lower().startswith("no known"):
            if verdict == Verdict.OK:
                verdict = Verdict.WARN
            reasons.append(str(d["errors"]))
        pool = _pool(session, host, PoolKind.ZFS, name, res)
        _pool_status(
            session,
            pool,
            attempt,
            when,
            health=health,
            verdict=verdict,
            reasons=reasons,
            raw=status.text if status is not None else lst.text,
            read_errors=d.get("read"),
            write_errors=d.get("write"),
            cksum_errors=d.get("cksum"),
            scrub=d.get("scan"),
        )
        n += 1
    return n


_BTRFS_STAT = re.compile(r"^\[(?P<dev>[^\]]+)\]\.(?P<kind>\w+)\s+(?P<n>\d+)\s*$")


def parse_btrfs_stats(text: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for line in text.splitlines():
        m = _BTRFS_STAT.match(line.strip())
        if m:
            totals[m.group("kind")] = totals.get(m.group("kind"), 0) + int(m.group("n"))
    return totals


def _ingest_btrfs(
    session: Session, host: Host, attempt: HostAttempt, env: Envelope, when: datetime, res: IngestResult
) -> int:
    mounts = env.frame("btrfs.mounts")
    if mounts is None or mounts.rc != 0:
        return 0
    n = 0
    for line in mounts.text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        target, uuid = parts[0], parts[1]
        stats = env.frame(f"btrfs.stats:{uuid}")
        scrub = env.frame(f"btrfs.scrub:{uuid}")
        totals = parse_btrfs_stats(stats.text) if stats is not None and stats.rc == 0 else {}
        reasons: list[str] = []
        verdict = Verdict.OK if stats is not None and stats.rc == 0 else Verdict.UNKNOWN
        if stats is not None and stats.rc != 0:
            reasons.append(f"btrfs device stats failed (rc {stats.rc})")
        corrupt = totals.get("corruption_errs", 0) + totals.get("generation_errs", 0)
        io = totals.get("write_io_errs", 0) + totals.get("read_io_errs", 0) + totals.get("flush_io_errs", 0)
        if corrupt:
            verdict = Verdict.FAIL
            reasons.append(f"{corrupt} corruption/generation errors")
        if io:
            if verdict == Verdict.OK:
                verdict = Verdict.WARN
            reasons.append(f"{io} I/O errors")
        scrub_text = scrub.text.strip() if scrub is not None and scrub.rc == 0 else None
        scrub_errors = btrfs_found_errors(scrub_text)
        if scrub_errors:
            if verdict in (Verdict.OK, Verdict.UNKNOWN):
                verdict = Verdict.WARN
            reasons.append(f"last scrub: {scrub_errors}")
        pool = _pool(session, host, PoolKind.BTRFS, target, res)
        _pool_status(
            session,
            pool,
            attempt,
            when,
            health="errors" if (corrupt or io) else ("ok" if verdict == Verdict.OK else None),
            verdict=verdict,
            reasons=reasons,
            raw=(stats.text if stats is not None else "") or None,
            read_errors=totals.get("read_io_errs"),
            write_errors=totals.get("write_io_errs"),
            cksum_errors=totals.get("corruption_errs"),
            scrub=scrub_text or None,
        )
        n += 1
    return n


_MD_HEAD = re.compile(r"^(md\d+)\s*:\s*(\S+)\s+(?:\(\S+\)\s+)?(\S+)")


def parse_mdstat(text: str) -> dict[str, dict[str, Any]]:
    """/proc/mdstat, per array: state word (active/inactive), level, the [UU_] map, and any resync line."""
    arrays: dict[str, dict[str, Any]] = {}
    cur: dict[str, Any] | None = None
    for line in text.splitlines():
        m = _MD_HEAD.match(line)
        if m:
            cur = {"state": m.group(2), "level": m.group(3), "map": None, "progress": None}
            arrays[m.group(1)] = cur
        elif cur is not None:
            mm = re.search(r"\[([U_]+)\]", line)
            if mm:
                cur["map"] = mm.group(1)
            if re.search(r"resync|recovery|reshape|check", line):
                cur["progress"] = line.strip()
    return arrays


def _ingest_md(
    session: Session, host: Host, attempt: HostAttempt, env: Envelope, when: datetime, res: IngestResult
) -> int:
    f = env.frame("mdstat")
    if f is None or f.rc != 0:
        return 0
    n = 0
    for name, d in parse_mdstat(f.text).items():
        reasons: list[str] = []
        verdict = Verdict.OK
        if d["state"] != "active":
            verdict = Verdict.FAIL
            reasons.append(f"array {d['state']}")
        if d["map"] and "_" in d["map"]:
            verdict = Verdict.FAIL
            reasons.append(f"degraded [{d['map']}]")
        if d["progress"]:
            if verdict == Verdict.OK:
                verdict = Verdict.WARN
            reasons.append(d["progress"])
        detail = env.frame(f"md.detail:{name}")
        pool = _pool(session, host, PoolKind.MD, name, res)
        _pool_status(
            session,
            pool,
            attempt,
            when,
            health="degraded" if (d["map"] and "_" in d["map"]) else d["state"],
            verdict=verdict,
            reasons=reasons,
            raw=f.text + ("\n" + detail.text if detail is not None and detail.rc == 0 else ""),
            scrub=d["progress"],
        )
        n += 1
    return n


def _ingest_pools(
    session: Session, host: Host, attempt: HostAttempt, env: Envelope, when: datetime, res: IngestResult
) -> int:
    seen = 0
    seen += _ingest_zfs(session, host, attempt, env, when, res)
    seen += _ingest_btrfs(session, host, attempt, env, when, res)
    seen += _ingest_md(session, host, attempt, env, when, res)
    return seen


# --------------------------------------------------------------------------- attrlog chunks


def _ingest_attrlog_chunk(session: Session, host: Host, f: Frame, tz: ZoneInfo, cfg: Config, res: IngestResult) -> int:
    file_name = f.name.partition(":")[2]
    try:
        name = attrlog.parse_name(file_name)
    except ValueError:
        res.warnings.append(f"{file_name}: not an attrlog name; ignored")
        return 0
    if name.kind != "ata":
        return 0  # SCSI/NVMe attrlogs: another format, not read yet
    cursor = session.scalar(
        select(AttrlogCursor).where(AttrlogCursor.host_id == host.id, AttrlogCursor.file_name == file_name)
    )
    if cursor is None:
        cursor = AttrlogCursor(host_id=host.id, file_name=file_name)
        session.add(cursor)
    offset = int(f.attrs.get("offset", "0") or 0)
    inode = int(f.attrs["inode"]) if f.attrs.get("inode", "").isdigit() else None
    text, consumed = attrlog.split_complete_lines(f.out)
    drive = attrlog.drive_for(session, name)
    result = attrlog.import_lines(
        session,
        drive=drive,
        tz=tz,
        lines=attrlog.iter_lines(text),
        cfg=cfg.status,
        host_id=host.id,
        after=cursor.last_ts if offset > 0 else None,
    )
    cursor.inode = inode
    cursor.offset = offset + consumed
    cursor.lines += result.lines
    if result.last_ts is not None:
        cursor.last_ts = result.last_ts
    cursor.updated_at = utcnow()
    if result.lines:
        log.info(
            "attrlog.chunk",
            host=host.name,
            file=file_name,
            lines=result.lines,
            added=result.added,
            offset=cursor.offset,
        )
    return result.lines

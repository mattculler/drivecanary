"""SQLAlchemy models. Timestamps are aware UTC datetimes stored as epoch seconds (`Epoch`).

The shape (docs/transport-design-2026-09-28.md §5): a drive is a thing with a serial; where it is plugged
in is a *sighting*; every reading of it is a *smart_run* with its attributes broken out into *attr_sample*
rows for trends; a host's every collection is an *attempt* with a reason when it failed, so silence always
has a cause on record.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class Epoch(TypeDecorator[datetime]):
    """An aware UTC datetime as integer seconds since the epoch: compact, sortable, and free of SQLite's
    string-datetime ambiguity. A naive datetime is refused rather than guessed at."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> int | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; drivecanary stores aware UTC datetimes only")
        return int(value.timestamp())

    def process_result_value(self, value: int | None, dialect: Any) -> datetime | None:
        return None if value is None else datetime.fromtimestamp(value, UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class HostState(StrEnum):
    PENDING = "pending"  # added, never collected
    OK = "ok"
    UNREACHABLE = "unreachable"  # ssh could not get there; retried
    BROKEN = "broken"  # got there and something on the host is wrong: auth, sudo, probe, key not restricted
    PAUSED = "paused"  # by hand: not tried, not counted
    RETIRED = "retired"  # gone for good; history kept


class Transport(StrEnum):
    PULL = "pull"
    PUSH = "push"


class FailureClass(StrEnum):
    UNREACHABLE = "unreachable"
    AUTH = "auth"
    HOSTKEY = "hostkey"
    KEY_NOT_RESTRICTED = "key_not_restricted"
    SUDO = "sudo"
    PROBE = "probe"
    TIMEOUT = "timeout"
    ENVELOPE = "envelope"
    TOO_LARGE = "too_large"
    UNKNOWN = "unknown"


class SampleSource(StrEnum):
    SMARTCTL = "smartctl"  # the probe's `smartctl -j -x`
    ATTRLOG = "attrlog"  # smartd's attribute log, imported or pulled
    LEGACY = "legacy"  # the 2017 text captures, if ever imported


class Verdict(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"  # nothing to judge by
    SKIPPED = "skipped"  # drive in standby, left asleep
    ERROR = "error"  # smartctl could not read the drive
    STALE = "stale"  # display only: the last reading is too old to trust


class PoolKind(StrEnum):
    ZFS = "zfs"
    BTRFS = "btrfs"
    MD = "md"


class Base(DeclarativeBase):
    pass


class Host(Base):
    __tablename__ = "host"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    address: Mapped[str] = mapped_column(String(255))
    ssh_user: Mapped[str | None] = mapped_column(String(64))  # None: [collect].ssh_user
    ssh_port: Mapped[int] = mapped_column(default=22)
    tz: Mapped[str | None] = mapped_column(String(64))  # Olson name; None: whatever the gate reports
    transport: Mapped[str] = mapped_column(String(8), default=Transport.PULL.value)
    state: Mapped[str] = mapped_column(String(16), default=HostState.PENDING.value)
    machine_id: Mapped[str | None] = mapped_column(String(64))
    hostkey: Mapped[str | None] = mapped_column(Text)  # the pinned known_hosts line (pull)
    push_token_hash: Mapped[str | None] = mapped_column(String(64))  # sha256 of the agent's token (push)
    gate_version: Mapped[int | None] = mapped_column(Integer)
    probe_version: Mapped[int | None] = mapped_column(Integer)
    os_release: Mapped[str | None] = mapped_column(String(128))
    smartctl_version: Mapped[str | None] = mapped_column(String(64))
    last_success_at: Mapped[datetime | None] = mapped_column(Epoch)
    last_attempt_at: Mapped[datetime | None] = mapped_column(Epoch)
    last_attempt_class: Mapped[str | None] = mapped_column(String(32))
    last_attempt_reason: Mapped[str | None] = mapped_column(Text)
    unreachable_since: Mapped[datetime | None] = mapped_column(Epoch)
    created_at: Mapped[datetime] = mapped_column(Epoch, default=utcnow)
    note: Mapped[str | None] = mapped_column(Text)

    sightings: Mapped[list[DriveSighting]] = relationship(back_populates="host")

    @property
    def active(self) -> bool:
        return self.state not in (HostState.PAUSED, HostState.RETIRED)


class Drive(Base):
    __tablename__ = "drive"
    __table_args__ = (UniqueConstraint("model_key", "serial_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    #: smartd's spelling of model and serial (every non-alphanumeric byte an underscore): the join key between
    #: an attrlog file name and what smartctl reports
    model_key: Mapped[str] = mapped_column(String(128))
    serial_key: Mapped[str] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(128))
    serial: Mapped[str | None] = mapped_column(String(128))
    wwn: Mapped[str | None] = mapped_column(String(32))
    protocol: Mapped[str | None] = mapped_column(String(16))  # ATA, NVMe, SCSI
    device_type: Mapped[str | None] = mapped_column(String(32))  # smartctl -d type: sat, nvme, scsi, usbjmicron...
    rotation_rate: Mapped[int | None] = mapped_column(Integer)  # rpm; 0 = solid state
    capacity_bytes: Mapped[int | None] = mapped_column(BigInteger)
    model_family: Mapped[str | None] = mapped_column(String(128))
    firmware: Mapped[str | None] = mapped_column(String(64))
    form_factor: Mapped[str | None] = mapped_column(String(32))
    identity_source: Mapped[str] = mapped_column(String(16), default="smartctl")  # smartctl | attrlog
    first_seen_at: Mapped[datetime] = mapped_column(Epoch, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(Epoch)
    retired: Mapped[bool] = mapped_column(default=False)
    note: Mapped[str | None] = mapped_column(Text)

    sightings: Mapped[list[DriveSighting]] = relationship(back_populates="drive")

    @property
    def kind(self) -> str:
        """hdd, ssd, nvme or unknown: what the icon and the columns are chosen by."""
        if (self.protocol or "").lower() == "nvme":
            return "nvme"
        if self.rotation_rate is None:
            return "unknown"
        return "ssd" if self.rotation_rate == 0 else "hdd"

    @property
    def label(self) -> str:
        return self.model or self.model_key.replace("_", " ")


class DriveSighting(Base):
    """Where a drive was plugged in: which host, as which device node, over what time."""

    __tablename__ = "drive_sighting"
    __table_args__ = (UniqueConstraint("drive_id", "host_id", "dev_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    drive_id: Mapped[int] = mapped_column(ForeignKey("drive.id"))
    host_id: Mapped[int] = mapped_column(ForeignKey("host.id"))
    dev_name: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(Epoch, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(Epoch, default=utcnow)
    current: Mapped[bool] = mapped_column(default=True)  # seen in the host's latest successful collection

    drive: Mapped[Drive] = relationship(back_populates="sightings")
    host: Mapped[Host] = relationship(back_populates="sightings")


class CollectionRun(Base):
    __tablename__ = "collection_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(Epoch, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(Epoch)
    trigger: Mapped[str] = mapped_column(String(16), default="timer")  # timer | manual | import
    hosts_expected: Mapped[int] = mapped_column(default=0)
    hosts_ok: Mapped[int] = mapped_column(default=0)
    hosts_failed: Mapped[int] = mapped_column(default=0)
    note: Mapped[str | None] = mapped_column(Text)


class HostAttempt(Base):
    """One try at one host. Every expected host gets one per run, with the class and reason when it failed."""

    __tablename__ = "host_attempt"
    __table_args__ = (
        Index("ix_host_attempt_host_started", "host_id", "started_at"),
        Index("ix_host_attempt_payload", "payload_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("collection_run.id"))
    host_id: Mapped[int] = mapped_column(ForeignKey("host.id"))
    started_at: Mapped[datetime] = mapped_column(Epoch, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(Epoch)
    transport: Mapped[str | None] = mapped_column(String(8))  # pull | push: how this one arrived
    ok: Mapped[bool] = mapped_column(default=False)
    failure_class: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    ssh_rc: Mapped[int | None] = mapped_column(Integer)
    payload_id: Mapped[str | None] = mapped_column(String(64))
    host_collected_at: Mapped[datetime | None] = mapped_column(Epoch)  # the host's own clock
    host_tz: Mapped[str | None] = mapped_column(String(64))
    drives_seen: Mapped[int] = mapped_column(default=0)
    bytes_received: Mapped[int] = mapped_column(default=0)


class SmartRun(Base):
    """One reading of one drive: the verdict, the typed health fields, and (for the probe's readings) the raw
    JSON, zlib-compressed. Attributes go to attr_sample."""

    __tablename__ = "smart_run"
    __table_args__ = (
        UniqueConstraint("drive_id", "source", "collected_at"),
        Index("ix_smart_run_drive_time", "drive_id", "collected_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    drive_id: Mapped[int] = mapped_column(ForeignKey("drive.id"))
    host_id: Mapped[int | None] = mapped_column(ForeignKey("host.id"))
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("host_attempt.id"))
    source: Mapped[str] = mapped_column(String(16))
    collected_at: Mapped[datetime] = mapped_column(Epoch)
    exit_status: Mapped[int | None] = mapped_column(Integer)
    passed: Mapped[bool | None] = mapped_column()
    standby: Mapped[bool] = mapped_column(default=False)
    temp_c: Mapped[int | None] = mapped_column(Integer)
    power_on_hours: Mapped[int | None] = mapped_column(Integer)
    power_cycles: Mapped[int | None] = mapped_column(Integer)
    ata_error_count: Mapped[int | None] = mapped_column(Integer)
    selftest_errors: Mapped[int | None] = mapped_column(Integer)
    selftest_last: Mapped[str | None] = mapped_column(String(128))
    nvme_percentage_used: Mapped[int | None] = mapped_column(Integer)
    nvme_available_spare: Mapped[int | None] = mapped_column(Integer)
    nvme_spare_threshold: Mapped[int | None] = mapped_column(Integer)
    nvme_media_errors: Mapped[int | None] = mapped_column(BigInteger)
    nvme_critical_warning: Mapped[int | None] = mapped_column(Integer)
    nvme_err_log_entries: Mapped[int | None] = mapped_column(BigInteger)
    nvme_unsafe_shutdowns: Mapped[int | None] = mapped_column(BigInteger)
    nvme_data_units_written: Mapped[int | None] = mapped_column(BigInteger)
    nvme_data_units_read: Mapped[int | None] = mapped_column(BigInteger)
    scsi_grown_defects: Mapped[int | None] = mapped_column(Integer)
    scsi_uncorrected_errors: Mapped[int | None] = mapped_column(Integer)
    messages: Mapped[str | None] = mapped_column(Text)  # JSON list of smartctl's messages
    raw_json: Mapped[bytes | None] = mapped_column(LargeBinary)  # zlib(smartctl -j -x output)
    verdict: Mapped[str] = mapped_column(String(8), default=Verdict.UNKNOWN.value)
    reasons: Mapped[str | None] = mapped_column(Text)  # one per line


class AttrSample(Base):
    """One ATA attribute at one instant. `raw` is the packed 48-bit raw value as the drive reports it;
    `display_raw()` in drivecanary.smart turns it into the number smartctl would print."""

    __tablename__ = "attr_sample"
    __table_args__ = (
        Index("ix_attr_sample_run", "run_id"),
        {"sqlite_with_rowid": False},
    )

    drive_id: Mapped[int] = mapped_column(ForeignKey("drive.id"), primary_key=True)
    attr_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collected_at: Mapped[datetime] = mapped_column(Epoch, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("smart_run.id"))
    value: Mapped[int | None] = mapped_column(Integer)  # normalized, 1..253
    worst: Mapped[int | None] = mapped_column(Integer)
    thresh: Mapped[int | None] = mapped_column(Integer)
    raw: Mapped[int] = mapped_column(BigInteger)
    raw_str: Mapped[str | None] = mapped_column(String(64))  # smartctl's rendering; None for attrlog rows
    when_failed: Mapped[str | None] = mapped_column(String(8))  # '', 'now', 'past'
    prefail: Mapped[bool | None] = mapped_column()


class Pool(Base):
    __tablename__ = "pool"
    __table_args__ = (UniqueConstraint("host_id", "kind", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("host.id"))
    kind: Mapped[str] = mapped_column(String(8))  # zfs | btrfs | md
    name: Mapped[str] = mapped_column(String(128))
    first_seen_at: Mapped[datetime] = mapped_column(Epoch, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(Epoch)
    retired: Mapped[bool] = mapped_column(default=False)


class PoolStatus(Base):
    __tablename__ = "pool_status"
    __table_args__ = (UniqueConstraint("pool_id", "collected_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pool.id"))
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("host_attempt.id"))
    collected_at: Mapped[datetime] = mapped_column(Epoch)
    health: Mapped[str | None] = mapped_column(String(32))  # ONLINE, DEGRADED, clean, active...
    read_errors: Mapped[int | None] = mapped_column(Integer)
    write_errors: Mapped[int | None] = mapped_column(Integer)
    cksum_errors: Mapped[int | None] = mapped_column(Integer)
    scrub: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(String(8), default=Verdict.UNKNOWN.value)
    reasons: Mapped[str | None] = mapped_column(Text)


class AttrlogCursor(Base):
    """How far into each of a host's smartd attrlog files the hub has read."""

    __tablename__ = "attrlog_cursor"
    __table_args__ = (UniqueConstraint("host_id", "file_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("host.id"))
    file_name: Mapped[str] = mapped_column(String(255))
    inode: Mapped[int | None] = mapped_column(BigInteger)
    offset: Mapped[int] = mapped_column(BigInteger, default=0)
    lines: Mapped[int] = mapped_column(default=0)
    last_ts: Mapped[datetime | None] = mapped_column(Epoch)
    updated_at: Mapped[datetime] = mapped_column(Epoch, default=utcnow)

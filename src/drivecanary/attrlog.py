"""smartd's attribute log: the years of history that were already on every host.

Debian builds smartd with the attribute log on, so every check (every 30 min by default) appends one line
to /var/lib/smartmontools/attrlog.<MODEL>-<SERIAL>.ata.csv: a local wall-clock timestamp, then `id;norm;raw;`
for every attribute, tab-separated (smartd(8), -A). There is no worst, threshold or flag column, the raw
value is the packed 48-bit one, and the file name is smartd's own spelling of the model and serial (every
non-alphanumeric byte an underscore), which is what a drive's `model_key`/`serial_key` are for. NVMe logs
exist only from smartmontools 7.5 and are not read here yet.

The same code imports a whole file by hand (`drivecanary import attrlog`) and the chunks the gate sends
past the hub's cursor on every collection.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from drivecanary.config import StatusConfig
from drivecanary.logging import get_logger
from drivecanary.models import AttrSample, Drive, SampleSource, SmartRun
from drivecanary.smart import display_raw
from drivecanary.status import judge_attrs
from drivecanary.timeutil import local_to_utc, utcnow

log = get_logger(__name__)

ATTRLOG_NAME = re.compile(
    r"^attrlog\.(?P<model>[A-Za-z0-9_]+)-(?P<serial>[A-Za-z0-9_]+)\.(?P<kind>ata|scsi|nvme)\.csv$"
)
_TS = "%Y-%m-%d %H:%M:%S"
BATCH = 2000


@dataclass(frozen=True)
class AttrlogName:
    model_key: str
    serial_key: str
    kind: str


def parse_name(file_name: str) -> AttrlogName:
    m = ATTRLOG_NAME.match(file_name.rsplit("/", 1)[-1])
    if not m:
        raise ValueError(f"not an attrlog file name: {file_name!r}")
    return AttrlogName(m.group("model"), m.group("serial"), m.group("kind"))


@dataclass
class AttrlogLine:
    ts_local: datetime  # naive wall clock, in the host's zone
    attrs: dict[int, tuple[int | None, int]] = field(default_factory=dict)  # id -> (normalized, raw)

    @property
    def raws(self) -> dict[int, int]:
        return {k: v[1] for k, v in self.attrs.items()}


def parse_line(line: str) -> AttrlogLine | None:
    """One attrlog line, or None for a blank or malformed one (a truncated last line, a stray header)."""
    line = line.strip()
    if not line:
        return None
    ts_text, _, rest = line.partition(";")
    try:
        ts = datetime.strptime(ts_text.strip(), _TS)
    except ValueError:
        return None
    out = AttrlogLine(ts)
    for cell in rest.split("\t"):
        cell = cell.strip().strip(";")
        if not cell:
            continue
        parts = cell.split(";")
        if len(parts) < 3:
            continue
        try:
            attr_id, norm, raw = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        out.attrs[attr_id] = (norm, raw)
    return out if out.attrs else None


def iter_lines(text: str) -> Iterator[AttrlogLine]:
    for raw_line in text.splitlines():
        parsed = parse_line(raw_line)
        if parsed is not None:
            yield parsed


def split_complete_lines(data: bytes) -> tuple[str, int]:
    """The complete lines of a chunk and how many bytes they took: a chunk read while smartd is appending
    may end mid-line, and that tail is left for the next collection."""
    end = data.rfind(b"\n")
    if end < 0:
        return "", 0
    return data[: end + 1].decode("utf-8", "replace"), end + 1


def line_health(line: AttrlogLine) -> tuple[int | None, int | None, int | None]:
    """(temperature, power-on hours, power cycles) from the attributes an attrlog line has."""
    temp = None
    for attr_id in (194, 190):
        if attr_id in line.attrs:
            temp = display_raw(attr_id, line.attrs[attr_id][1])
            break
    poh = display_raw(9, line.attrs[9][1]) if 9 in line.attrs else None
    cycles = line.attrs[12][1] if 12 in line.attrs else None
    return temp, poh, cycles


def drive_for(session: Session, name: AttrlogName) -> Drive:
    """The drive an attrlog file belongs to, created from the file name if it has never been seen by
    smartctl (a first import before the first collection: atlas, today)."""
    drive = session.scalar(select(Drive).where(Drive.model_key == name.model_key, Drive.serial_key == name.serial_key))
    if drive is None:
        drive = Drive(
            model_key=name.model_key,
            serial_key=name.serial_key,
            protocol="ATA" if name.kind == "ata" else name.kind.upper(),
            identity_source="attrlog",
        )
        session.add(drive)
        session.flush()
        log.info("attrlog.drive_created", model_key=name.model_key, serial_key=name.serial_key)
    return drive


@dataclass
class ImportResult:
    lines: int = 0
    added: int = 0
    duplicates: int = 0
    last_ts: datetime | None = None  # UTC of the last line seen, duplicate or not


def import_lines(
    session: Session,
    *,
    drive: Drive,
    tz: ZoneInfo,
    lines: Iterable[AttrlogLine],
    cfg: StatusConfig,
    host_id: int | None = None,
    after: datetime | None = None,
    source: str = SampleSource.ATTRLOG.value,
) -> ImportResult:
    """Store attrlog lines as smart_run + attr_sample rows. Idempotent: a line whose instant is already
    stored for this drive and source is skipped, so a file can be imported again, or overlap a pulled chunk.

    `after` is the UTC instant of the line before the first one here (the cursor's last_ts), which is what
    resolves the repeated hour of a DST fall-back; a clock that truly went backwards is stored as read.
    """
    res = ImportResult()
    existing = {
        int(ts.timestamp())
        for ts in session.scalars(
            select(SmartRun.collected_at).where(SmartRun.drive_id == drive.id, SmartRun.source == source)
        )
    }
    run_rows: list[dict[str, object]] = []
    attr_rows: list[list[tuple[int, int | None, int]]] = []
    prev = after

    def flush() -> None:
        if not run_rows:
            return
        result = session.execute(insert(SmartRun).returning(SmartRun.id, sort_by_parameter_order=True), run_rows)
        ids = [row[0] for row in result]
        samples: list[dict[str, object]] = []
        for run_id, run, attrs in zip(ids, run_rows, attr_rows, strict=True):
            samples.extend(
                {
                    "drive_id": drive.id,
                    "attr_id": attr_id,
                    "collected_at": run["collected_at"],
                    "run_id": run_id,
                    "value": norm,
                    "raw": raw,
                }
                for attr_id, norm, raw in attrs
            )
        if samples:
            session.execute(insert(AttrSample), samples)
        res.added += len(run_rows)
        run_rows.clear()
        attr_rows.clear()

    for line in lines:
        res.lines += 1
        when = local_to_utc(line.ts_local, tz, after=prev)
        prev = when
        res.last_ts = when
        epoch = int(when.timestamp())
        if epoch in existing:
            res.duplicates += 1
            continue
        existing.add(epoch)
        temp, poh, cycles = line_health(line)
        verdict, reasons = judge_attrs(line.raws, temp, cfg)
        run_rows.append(
            {
                "drive_id": drive.id,
                "host_id": host_id,
                "source": source,
                "collected_at": when,
                "standby": False,
                "temp_c": temp,
                "power_on_hours": poh,
                "power_cycles": cycles,
                "verdict": verdict.value,
                "reasons": "\n".join(reasons) or None,
            }
        )
        attr_rows.append([(attr_id, norm, raw) for attr_id, (norm, raw) in line.attrs.items()])
        if len(run_rows) >= BATCH:
            flush()
    flush()
    if res.added:
        drive.last_seen_at = max(drive.last_seen_at or res.last_ts or utcnow(), res.last_ts or utcnow())
    return res


def import_file(
    session: Session,
    path: str,
    *,
    tz: ZoneInfo,
    cfg: StatusConfig,
    host_id: int | None = None,
) -> tuple[Drive, ImportResult]:
    """`drivecanary import attrlog FILE`: the whole file, the drive found or made from its name."""
    name = parse_name(path)
    drive = drive_for(session, name)
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    result = import_lines(session, drive=drive, tz=tz, lines=iter_lines(text), cfg=cfg, host_id=host_id)
    log.info(
        "attrlog.imported", file=path, drive_id=drive.id, **{k: v for k, v in vars(result).items() if k != "last_ts"}
    )
    return drive, result

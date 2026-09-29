"""Which drives a pool is made of.

A pool's status names its members the way the host names block devices: `sda`, `/dev/sdb1`,
`nvme0n1p2`, `ata-ST20000NM007D-3DJ103_ZXA00001`, `/dev/mapper/crypt1`. A drive is known by what smartctl
read from it. The two are brought together, in this order, by

1. the host's block device table (lsblk): the member, up through partitions and mappings to the disk under
   it, and that disk's serial;
2. a drive's serial appearing in the member's name, as it does in /dev/disk/by-id names;
3. the device name, with the partition taken off, against where the drive was last seen on the host.

A member that none of these finds is still listed, by the name the pool gave it, without a link: a pool
with a disk this page cannot account for should look like one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from drivecanary.models import Drive, DriveSighting, Host, Pool, PoolKind, PoolStatus
from drivecanary.smart import sanitize_key

#: lines of `zpool status` that name a grouping, not a device
_ZFS_GROUP = re.compile(r"^(mirror|raidz[123]?|draid\S*|spare|replacing|indirect)-\d+$")
_ZFS_SECTION = {"logs", "cache", "spares", "special", "dedup", "NAME"}
_ZFS_LINE = re.compile(
    r"^\s+(?P<name>\S+)\s+(?P<state>ONLINE|DEGRADED|FAULTED|UNAVAIL|REMOVED|OFFLINE|AVAIL|INUSE)"
    r"(?:\s+(?P<read>\d+)\s+(?P<write>\d+)\s+(?P<cksum>\d+))?"
)
_BTRFS_LINE = re.compile(r"^\[(?P<dev>[^\]]+)\]\.(?P<kind>\w+)\s+(?P<n>\d+)\s*$")
_MD_LINE = re.compile(r"^(md\d+)\s*:\s*\S+\s+(?:\(\S+\)\s+)?\S+\s+(.*)$")
_MD_MEMBER = re.compile(r"(\S+?)\[\d+\](\([A-Z]\))?")


@dataclass
class Member:
    name: str  # as the pool names it
    state: str | None = None
    read_errors: int | None = None
    write_errors: int | None = None
    cksum_errors: int | None = None
    drive: Drive | None = None
    dev_name: str | None = None  # where the drive is on the host, when it was found
    how: str | None = None  # which of the three ways found it

    @property
    def errors(self) -> int:
        return (self.read_errors or 0) + (self.write_errors or 0) + (self.cksum_errors or 0)


def zfs_members(text: str, pool_name: str) -> list[Member]:
    out: list[Member] = []
    inside = False  # this pool's part of the text
    config = False  # and, in it, the table of devices: "state: ONLINE" above it is no device
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("pool:"):
            inside, config = s[5:].strip() == pool_name, False
            continue
        if s.startswith("config:"):
            config = True
            continue
        if s.startswith("errors:"):
            config = False
        if not (inside and config):
            continue
        m = _ZFS_LINE.match(line)
        if not m:
            continue
        name = m.group("name")
        if name == pool_name or name in _ZFS_SECTION or _ZFS_GROUP.match(name):
            continue
        read, write, cksum = (int(m.group(k)) if m.group(k) is not None else None for k in ("read", "write", "cksum"))
        out.append(Member(name, m.group("state"), read_errors=read, write_errors=write, cksum_errors=cksum))
    return out


def btrfs_members(text: str) -> list[Member]:
    found: dict[str, Member] = {}
    for line in text.splitlines():
        m = _BTRFS_LINE.match(line.strip())
        if not m:
            continue
        member = found.setdefault(m.group("dev"), Member(m.group("dev"), read_errors=0, write_errors=0, cksum_errors=0))
        n = int(m.group("n"))
        kind = m.group("kind")
        if kind == "read_io_errs":
            member.read_errors = (member.read_errors or 0) + n
        elif kind in ("write_io_errs", "flush_io_errs"):
            member.write_errors = (member.write_errors or 0) + n
        else:
            member.cksum_errors = (member.cksum_errors or 0) + n
    return list(found.values())


def md_members(text: str, array: str) -> list[Member]:
    for line in text.splitlines():
        m = _MD_LINE.match(line)
        if m and m.group(1) == array:
            flags = {"(F)": "faulty", "(S)": "spare", "(W)": "write-mostly", "(R)": "replacement"}
            return [Member(name, flags.get(flag or "", "active")) for name, flag in _MD_MEMBER.findall(m.group(2))]
    return []


def members_of(pool: Pool, status: PoolStatus | None) -> list[Member]:
    text = (status.raw if status is not None else None) or ""
    if pool.kind == PoolKind.ZFS.value:
        return zfs_members(text, pool.name)
    if pool.kind == PoolKind.BTRFS.value:
        return btrfs_members(text)
    if pool.kind == PoolKind.MD.value:
        return md_members(text, pool.name)
    return []


# --------------------------------------------------------------------------- finding the drive


@dataclass
class BlockTable:
    """lsblk's rows, by kernel name and by path."""

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    paths: dict[str, str] = field(default_factory=dict)  # /dev/mapper/x, /dev/sda1 or a bare name -> kernel name

    @classmethod
    def parse(cls, text: str | None) -> BlockTable:
        table = cls()
        if not text:
            return table
        try:
            doc = json.loads(text)
        except ValueError:
            return table

        def walk(rows: Any, parent: str | None) -> None:
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                kname = str(row.get("kname") or row.get("name") or "")
                if not kname:
                    continue
                row = dict(row)
                if not row.get("pkname") and parent:
                    row["pkname"] = parent  # an older lsblk nests instead of naming the parent
                table.rows.setdefault(kname, row)
                for key in (kname, row.get("name"), row.get("path"), f"/dev/{kname}"):
                    if key:
                        table.paths.setdefault(str(key), kname)
                walk(row.get("children"), kname)

        walk(doc.get("blockdevices") if isinstance(doc, dict) else None, None)
        return table

    def disk_under(self, member: str) -> dict[str, Any] | None:
        kname = self.paths.get(member) or self.paths.get(member.rsplit("/", 1)[-1])
        seen: set[str] = set()
        while kname and kname not in seen:
            seen.add(kname)
            row = self.rows.get(kname)
            if row is None:
                return None
            if row.get("type") == "disk" or not row.get("pkname"):
                return row
            kname = str(row["pkname"])
        return None


_PARTITION = (
    (re.compile(r"^(nvme\d+)n\d+(p\d+)?$"), r"\1"),  # nvme0n1p2: smartctl names the controller, nvme0
    (re.compile(r"^(nda|nvd)(\d+)((p|s)\d+[a-z]?)?$"), r"nvme\2"),  # FreeBSD's names for the same
    (re.compile(r"^((?:sd|vd|xvd|hd)[a-z]+)\d*$"), r"\1"),
    (re.compile(r"^((?:ada|da)\d+)((p|s)\d+[a-z]?)?$"), r"\1"),
)


def disk_name(member: str) -> str:
    """`sda1` -> `sda`, `nvme0n1p2` -> `nvme0`, `ada0p3` -> `ada0`: the name smartctl knows the disk by."""
    base = member.rsplit("/", 1)[-1]
    for pattern, to in _PARTITION:
        if pattern.match(base):
            return pattern.sub(to, base)
    return base


def resolve(members: list[Member], sightings: list[DriveSighting], blocks: BlockTable) -> list[Member]:
    by_serial = {s.drive.serial_key: s for s in sightings if s.drive.serial_key}
    by_dev = {s.dev_name.rsplit("/", 1)[-1]: s for s in sightings}
    for m in members:
        found: DriveSighting | None = None
        disk = blocks.disk_under(m.name)
        if disk is not None and disk.get("serial"):
            found = by_serial.get(sanitize_key(str(disk["serial"]).strip()))
            m.how = "block device table" if found else None
        if found is None:
            squashed = sanitize_key(m.name)
            for key, s in by_serial.items():
                if len(key) >= 6 and key in squashed:
                    found, m.how = s, "serial in the name"
                    break
        if found is None:
            name = disk_name(str(disk.get("kname")) if disk is not None else m.name)
            found = by_dev.get(name)
            m.how = "device name" if found else None
        if found is not None:
            m.drive, m.dev_name = found.drive, found.dev_name
    return members


def pool_members(session: Session, pool: Pool, status: PoolStatus | None) -> tuple[list[Member], list[DriveSighting]]:
    """(the pool's members, each with its drive where one was found; the host's other current drives)."""
    host = session.get(Host, pool.host_id)
    sightings = list(
        session.scalars(
            select(DriveSighting)
            .where(DriveSighting.host_id == pool.host_id, DriveSighting.current)
            .order_by(DriveSighting.dev_name)
        )
    )
    blocks = BlockTable.parse(host.block_devices if host is not None else None)
    members = resolve(members_of(pool, status), sightings, blocks)
    taken = {m.drive.id for m in members if m.drive is not None}
    return members, [s for s in sightings if s.drive_id not in taken]

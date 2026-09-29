"""Which drives a pool is made of: what its status names, and the three ways a name finds a drive."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from drivecanary import pools, queries
from drivecanary.collect import SshResult, collect
from drivecanary.config import Config
from drivecanary.models import Drive, DriveSighting, Host, Pool
from tests.conftest import LSBLK_JSON, ZPOOL_STATUS, ProbeEnv

BY_ID = """  pool: tank
 state: ONLINE
config:

\tNAME                                        STATE     READ WRITE CKSUM
\ttank                                        ONLINE       0     0     0
\t  raidz1-0                                  ONLINE       0     0     0
\t    ata-ST20000NM007D-3DJ103_ZXA00001       ONLINE       0     0     0
\t    ata-ST20000NM007D-3DJ103_ZXA00003-part1 ONLINE       0     0     2
\t    wwn-0x5000c500e1234567                  ONLINE       0     0     0
\tlogs
\t  nvme0n1p2                                 ONLINE       0     0     0
\tspares
\t  sdq                                       AVAIL

errors: No known data errors
"""


def names(members: list[pools.Member]) -> list[str]:
    return [m.name for m in members]


def test_zfs_members_are_the_devices_not_the_groupings() -> None:
    tank = pools.zfs_members(ZPOOL_STATUS, "tank")
    assert names(tank) == ["sda", "sdb"] and all(m.state == "ONLINE" and m.errors == 0 for m in tank)
    backup = pools.zfs_members(ZPOOL_STATUS, "backup")
    assert names(backup) == ["sdc", "sdd"]
    assert backup[0].cksum_errors == 7 and backup[1].state == "REMOVED"
    found = pools.zfs_members(BY_ID, "tank")
    assert names(found) == [
        "ata-ST20000NM007D-3DJ103_ZXA00001",
        "ata-ST20000NM007D-3DJ103_ZXA00003-part1",
        "wwn-0x5000c500e1234567",
        "nvme0n1p2",
        "sdq",
    ]
    assert found[1].cksum_errors == 2 and found[4].state == "AVAIL" and found[4].read_errors is None
    assert pools.zfs_members(BY_ID, "another") == []


def test_btrfs_and_md_members() -> None:
    stats = "[/dev/sde].write_io_errs    3\n[/dev/sde].read_io_errs     0\n[/dev/mapper/vault].corruption_errs  1\n"
    found = pools.btrfs_members(stats)
    assert names(found) == ["/dev/sde", "/dev/mapper/vault"]
    assert (found[0].write_errors, found[0].read_errors, found[1].cksum_errors) == (3, 0, 1)
    mdstat = (
        "Personalities : [raid1]\nmd0 : active raid1 sdb1[1] sda1[0](F)\n"
        "      976630464 blocks [2/1] [_U]\nmd1 : active raid1 sdc1[0]\n"
    )
    found = pools.md_members(mdstat, "md0")
    assert [(m.name, m.state) for m in found] == [("sdb1", "active"), ("sda1", "faulty")]


def test_disk_name() -> None:
    for member, disk in (
        ("sda1", "sda"),
        ("/dev/sdb", "sdb"),
        ("sdaa12", "sdaa"),
        ("nvme0n1p2", "nvme0"),
        ("/dev/nvme1n1", "nvme1"),
        ("ada0p3", "ada0"),
        ("nda0p2", "nvme0"),
        ("md0", "md0"),
    ):
        assert pools.disk_name(member) == disk, member


def _sighting(dev: str, model: str, serial: str) -> DriveSighting:
    drive = Drive(id=hash(serial) % 10_000, model_key=model, serial_key=serial, serial=serial, model=model)
    return DriveSighting(dev_name=dev, drive=drive, drive_id=drive.id)


def test_the_three_ways_a_member_finds_its_drive() -> None:
    seen = [
        _sighting("/dev/sda", "ST20000NM007D_3DJ103", "ZXA00001"),
        _sighting("/dev/sdb", "ST20000NM007D_3DJ103", "ZXA00003"),
        _sighting("/dev/nvme0", "INTEL_SSDPEKNW010T8", "BTNH93710FS91P0B"),
        _sighting("/dev/sdf", "WDC_WD140EDFZ", "9RK1XXXX"),
    ]
    blocks = pools.BlockTable.parse(LSBLK_JSON)
    found = {
        m.name: m
        for m in pools.resolve(
            pools.zfs_members(BY_ID, "tank")
            + pools.btrfs_members(
                "[/dev/mapper/vault].write_io_errs 0\n[/dev/sda1].write_io_errs 0\n[/dev/sdb].write_io_errs 0\n"
            ),
            seen,
            blocks,
        )
    }
    by_id = found["ata-ST20000NM007D-3DJ103_ZXA00001"]
    assert by_id.drive is seen[0].drive and by_id.how == "serial in the name"
    assert found["ata-ST20000NM007D-3DJ103_ZXA00003-part1"].drive is seen[1].drive
    assert found["wwn-0x5000c500e1234567"].drive is None, "nothing says whose that is: listed, not linked"
    # through the partition to the disk, and the disk's serial
    assert found["nvme0n1p2"].drive is seen[2].drive and found["nvme0n1p2"].how == "block device table"
    # through the mapping, the partition and the disk
    assert found["/dev/mapper/vault"].drive is seen[2].drive and found["/dev/mapper/vault"].dev_name == "/dev/nvme0"
    # the table's serial wins over the name: sda is the WDC here, whatever is at /dev/sda in the sightings
    assert found["/dev/sda1"].drive is seen[3].drive
    # the table knows sdb's serial, and no drive has it: fall back to the name
    assert found["/dev/sdb"].drive is seen[1].drive and found["/dev/sdb"].how == "device name"
    assert found["sdq"].drive is None


def test_without_a_block_table_names_still_work() -> None:
    seen = [_sighting("/dev/ada0", "SAMSUNG_SSD_PM830", "S0EXAMPL000000"), _sighting("/dev/nvme0", "X", "Y123456")]
    blocks = pools.BlockTable.parse(None)
    found = pools.resolve([pools.Member("ada0p3"), pools.Member("nda0p1"), pools.Member("gpt/zfs0")], seen, blocks)
    assert found[0].drive is seen[0].drive and found[1].drive is seen[1].drive and found[2].drive is None
    assert pools.BlockTable.parse("not json").rows == {} and pools.BlockTable.parse("[]").rows == {}


def test_an_older_lsblk_that_nests_children() -> None:
    nested = (
        '{"blockdevices": [{"kname": "sda", "type": "disk", "serial": "S1",'
        ' "children": [{"kname": "sda1", "type": "part"}]}]}'
    )
    disk = pools.BlockTable.parse(nested).disk_under("/dev/sda1")
    assert disk is not None and disk["kname"] == "sda" and disk["serial"] == "S1"


def test_pool_members_from_a_collection(cfg: Config, factory: sessionmaker[Session], probe_env: ProbeEnv) -> None:
    with factory() as s:
        s.add(Host(name="atlas", address="atlas"))
        s.commit()
    envelope = probe_env.run_gate("drivecanary-collect").stdout
    collect(cfg, factory, runner=lambda h, a: SshResult(0, envelope, b""))
    with factory() as s:
        host = s.scalar(select(Host))
        assert host is not None and host.block_devices and '"nvme0n1p2"' in host.block_devices
        tank = s.scalar(select(Pool).where(Pool.name == "tank"))
        assert tank is not None
        members, others = pools.pool_members(s, tank, queries.latest_pool_status(s, tank.id))
        assert [(m.name, m.dev_name, m.how) for m in members] == [
            ("sda", "/dev/sda", "block device table"),
            ("sdb", "/dev/sdb", "block device table"),
        ]
        assert [o.dev_name for o in others] == ["/dev/nvme0"]
        backup = s.scalar(select(Pool).where(Pool.name == "backup"))
        assert backup is not None
        members, others = pools.pool_members(s, backup, queries.latest_pool_status(s, backup.id))
        assert [m.name for m in members] == ["sdc", "sdd"] and all(m.drive is None for m in members)
        assert len(others) == 3

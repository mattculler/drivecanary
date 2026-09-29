"""The real shell scripts, run against shims: the envelope they produce is what the hub ingests."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from drivecanary.config import Config
from drivecanary.envelope import parse_envelope
from drivecanary.ingest import ingest_envelope
from drivecanary.models import (
    AttrlogCursor,
    AttrSample,
    Drive,
    DriveSighting,
    Host,
    HostAttempt,
    Pool,
    PoolStatus,
    SmartRun,
)
from tests.conftest import HOST_DIR, ProbeEnv


def test_gate_ping_and_refusals(probe_env: ProbeEnv) -> None:
    cp = probe_env.run_gate("drivecanary-ping")
    assert cp.returncode == 0 and cp.stdout == b"DRIVECANARY-PONG gate_version=2\n"
    for bad in (
        "id",
        "sh",
        "drivecanary-collect x",
        "drivecanary-collect ../etc/passwd=0",
        "drivecanary-collect a.csv=1x",
    ):
        cp = probe_env.run_gate(bad)
        assert cp.returncode == 64, bad
        assert cp.stdout == b""


def test_probe_answers_help_without_running(probe_env: ProbeEnv) -> None:
    import subprocess

    for word in ("help", "--help", "-h"):
        cp = subprocess.run(["sh", str(probe_env.probe), word], capture_output=True, text=True, check=False)
        assert cp.returncode == 0 and "usage" in cp.stdout


def test_collect_envelope_ingests(probe_env: ProbeEnv, session: Session, cfg: Config) -> None:
    cp = probe_env.run_gate("drivecanary-collect")
    assert cp.returncode == 0, cp.stderr
    env = parse_envelope(cp.stdout)
    assert env.complete and env.probe_ran and env.header["gate_version"] == "2"
    names = [f.name for f in env.frames]
    assert "smartctl.scan" in names and "smartctl.dev:/dev/sda:sat" in names and "smartctl.dev:/dev/nvme0:nvme" in names
    assert "smartctl.dev:/dev/sr0:scsi" not in names, "a device with open_error is left alone"
    assert len(names) == len(set(names)), "every device is read once"
    assert "zpool.list" in names and "btrfs.stats:1234-uuid" in names and "lsblk" in names
    assert env.frame("smartd.conf") is not None and b"BEGIN drivecanary self-tests" in env.frame("smartd.conf").out
    assert env.frame("smartd.active") is not None and env.frame("smartd.active").text == "active\n"
    assert env.frame("probe.exit") is not None and env.frame("probe.exit").rc == 0
    attr = env.prefixed("attrlog:")[0]
    assert attr.attrs["offset"] == "0" and int(attr.attrs["size"]) == len(attr.out)

    host = Host(name="atlas", address="atlas.domain", tz="America/New_York")
    session.add(host)
    session.flush()
    attempt = HostAttempt(host_id=host.id)
    session.add(attempt)
    session.flush()
    res = ingest_envelope(session, host=host, attempt=attempt, env=env, cfg=cfg)
    session.commit()
    assert res.drives == 3 and res.runs == 3 and res.pools == 3 and res.attrlog_lines == 20
    assert host.smartctl_version == "7.4" and host.gate_version == 2 and host.probe_version == 3 and host.machine_id

    drives = {d.serial_key: d for d in session.scalars(select(Drive))}
    assert len(drives) == 4  # sda, sdb, nvme0 from smartctl; the attrlog drive from its file name
    runs = {r.drive_id: r for r in session.scalars(select(SmartRun).where(SmartRun.source == "smartctl"))}
    by_dev = {s.dev_name: s for s in session.scalars(select(DriveSighting))}
    assert set(by_dev) == {"/dev/sda", "/dev/sdb", "/dev/nvme0"} and all(s.current for s in by_dev.values())
    assert runs[by_dev["/dev/sdb"].drive_id].verdict == "fail" and runs[by_dev["/dev/sda"].drive_id].verdict == "ok"
    nvme = runs[by_dev["/dev/nvme0"].drive_id]
    assert nvme.nvme_percentage_used is not None and nvme.raw_json is not None
    assert session.scalar(select(func.count()).select_from(AttrSample).where(AttrSample.raw_str.is_not(None))) > 0

    pools = {(p.kind, p.name): p for p in session.scalars(select(Pool))}
    assert set(pools) == {("zfs", "tank"), ("zfs", "backup"), ("btrfs", "/mnt/btr")}
    statuses = {s.pool_id: s for s in session.scalars(select(PoolStatus))}
    assert statuses[pools[("zfs", "tank")].id].verdict == "ok"
    bad = statuses[pools[("zfs", "backup")].id]
    assert bad.verdict == "fail" and bad.health == "DEGRADED" and bad.cksum_errors == 7
    btr = statuses[pools[("btrfs", "/mnt/btr")].id]
    assert btr.verdict == "warn" and btr.write_errors == 3 and btr.scrub and "finished" in btr.scrub

    cursor = session.scalar(select(AttrlogCursor))
    assert cursor is not None and cursor.offset == int(attr.attrs["size"]) and cursor.lines == 20
    last_line = (probe_env.attrlog_dir / cursor.file_name).read_text().splitlines()[-1]
    expected = datetime.strptime(last_line.split(";", 1)[0], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=ZoneInfo("America/New_York")
    )
    assert cursor.last_ts == expected.astimezone(UTC)

    # second collection: the gate is told the offset and sends nothing new; nothing is duplicated
    cp = probe_env.run_gate(f"drivecanary-collect {cursor.file_name}={cursor.offset}")
    env2 = parse_envelope(cp.stdout)
    attr2 = env2.prefixed("attrlog:")[0]
    assert attr2.attrs["offset"] == str(cursor.offset) and attr2.out == b""
    attempt2 = HostAttempt(host_id=host.id)
    session.add(attempt2)
    session.flush()
    res2 = ingest_envelope(session, host=host, attempt=attempt2, env=env2, cfg=cfg)
    assert res2.attrlog_lines == 0
    assert session.scalar(select(func.count(SmartRun.id)).where(SmartRun.source == "attrlog")) == 20


def test_a_host_without_a_zone_uses_what_it_reports_then_the_default(
    probe_env: ProbeEnv, session: Session, cfg: Config
) -> None:
    from drivecanary.ingest import _host_zone

    env = parse_envelope(probe_env.run_gate("drivecanary-collect").stdout)
    host = Host(name="h", address="h")
    warnings: list[str] = []
    env.header["tz"] = "Europe/Berlin"
    assert str(_host_zone(host, env, cfg, warnings)) == "Europe/Berlin"
    host.tz = "Asia/Tokyo"
    assert str(_host_zone(host, env, cfg, warnings)) == "Asia/Tokyo"
    host.tz = None
    env.header["tz"] = ""
    assert str(_host_zone(host, env, cfg, warnings)) == "America/New_York"
    env.header["tz"] = "Not/AZone"
    assert str(_host_zone(host, env, cfg, warnings)) == "America/New_York" and warnings


def test_a_second_probe_is_turned_away_while_one_runs(probe_env: ProbeEnv) -> None:
    import os
    import subprocess
    import time

    slow = '#!/bin/sh\ncase "$1" in --version) sleep 2 ;; esac\necho "{}"\n'
    (probe_env.shims / "smartctl").write_text(slow)
    env = dict(os.environ, PATH=f"{probe_env.shims}:{os.environ['PATH']}")
    first = subprocess.Popen(["sh", str(probe_env.probe)], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    time.sleep(0.7)
    second = subprocess.run(["sh", str(probe_env.probe)], env=env, capture_output=True, text=True, check=False)
    out, _ = first.communicate(timeout=60)
    assert second.returncode == 75 and "another probe is still running" in second.stderr and second.stdout == ""
    assert first.returncode == 0 and out.rstrip().splitlines()[-1].startswith(b"PROBE-END"), (
        "the first was not disturbed"
    )
    third = subprocess.run(["sh", str(probe_env.probe)], env=env, capture_output=True, text=True, check=False)
    assert third.returncode == 0, "and the lock is free again afterwards"
    assert "LOCKDIR=$TMP" not in (HOST_DIR / "probe").read_text(), (
        "a lock in the run's own directory is never contended"
    )


def test_the_last_device_in_a_scan_is_read_once(probe_env: ProbeEnv) -> None:
    """The brace that closes the whole scan printed the last device again, unless that device had failed to
    open: atlas's last drive was read twice every collection and the second reading thrown away."""
    scan = probe_env.shims.parent / "scan.json"
    text = scan.read_text()
    cut = text.index('    {\n      "name": "/dev/sr0"')
    scan.write_text(text[:cut].rstrip().rstrip(",") + "\n  ]\n}\n")
    names = [f.name for f in parse_envelope(probe_env.run_gate("drivecanary-collect").stdout).frames]
    devices = [n for n in names if n.startswith("smartctl.dev:")]
    assert devices == ["smartctl.dev:/dev/sda:sat", "smartctl.dev:/dev/sdb:sat", "smartctl.dev:/dev/nvme0:nvme"]


def test_devices_conf_skips_and_adds(probe_env: ProbeEnv) -> None:
    probe_env.devices_conf.write_text("# comment\n/dev/sdb skip\n/dev/sdz scsi\n")
    env = parse_envelope(probe_env.run_gate("drivecanary-collect").stdout)
    names = [f.name for f in env.frames]
    assert "smartctl.dev:/dev/sdb:sat" not in names and "smartctl.dev:/dev/sdz:scsi" in names


def test_sudo_failure_is_reported_in_the_envelope(probe_env: ProbeEnv, session: Session, cfg: Config) -> None:
    (probe_env.shims / "sudo").write_text("#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n")
    env = parse_envelope(probe_env.run_gate("drivecanary-collect").stdout)
    assert env.complete and not env.probe_ran
    pe = env.frame("probe.exit")
    assert pe is not None and pe.rc == 1 and b"password" in pe.err
    from drivecanary.ingest import IngestError

    host = Host(name="h", address="h")
    session.add(host)
    session.flush()
    attempt = HostAttempt(host_id=host.id)
    session.add(attempt)
    session.flush()
    try:
        ingest_envelope(session, host=host, attempt=attempt, env=env, cfg=cfg)
    except IngestError as e:
        assert e.failure_class.value == "sudo"
    else:
        raise AssertionError("expected IngestError")

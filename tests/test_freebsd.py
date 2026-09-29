"""The host scripts on FreeBSD, as OPNsense runs them: root's cron, no sudo, no /proc, no lsblk, no smartd.

The shims answer what a real OPNsense 25.7 router answered; the scripts are the real ones.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from drivecanary.config import Config
from drivecanary.envelope import parse_envelope
from drivecanary.models import Drive, DriveSighting, HostState, Pool, SmartRun
from tests.conftest import ProbeEnv
from tests.test_agent import _host, _push_host, hub  # noqa: F401  (hub is a fixture)


def test_envelope_from_a_freebsd_host(probe_env: ProbeEnv) -> None:
    probe_env.become_opnsense()
    cp = probe_env.run_gate("drivecanary-collect")
    assert cp.returncode == 0, cp.stderr
    env = parse_envelope(cp.stdout)
    assert env.complete and env.probe_ran
    probe_exit = env.frame("probe.exit")
    assert probe_exit is not None and probe_exit.rc == 0 and probe_exit.err == b"", "run directly: already root"
    h = env.header
    assert h["machine_id"] == "000000000000000000000000c0ffee00"
    assert h["boot_id"] == "1789678450", "the second it booted, not the microseconds beside it"
    assert h["tz"] == "Europe/Amsterdam", "from OPNsense's own config: /etc/localtime is a copy there"
    assert len(h["payload_id"]) == 32 and int(h["payload_id"], 16) >= 0
    names = [f.name for f in env.frames]
    assert names.count("smartctl.dev:/dev/ada0:atacam") == 1, "the last device in a scan is read once"
    assert "geom.disks" in names and "lsblk" not in names and "mdstat" not in names
    assert not env.prefixed("attrlog:") and not env.prefixed("btrfs.")
    release = env.frame("os.release")
    assert (
        release is not None and release.text == 'PRETTY_NAME="OPNsense 25.7.10_10 (amd64), FreeBSD 14.2-RELEASE-p11"\n'
    )
    dev = env.frame("smartctl.dev:/dev/ada0:atacam")
    assert dev is not None and dev.rc == 0 and dev.out.startswith(b"{")


def test_payload_ids_differ(probe_env: ProbeEnv) -> None:
    ids = {parse_envelope(probe_env.run_gate("drivecanary-collect").stdout).header["payload_id"] for _ in range(3)}
    assert len(ids) == 3


def test_an_nvme_disk_is_asked_by_its_controller(probe_env: ProbeEnv) -> None:
    probe_env.become_opnsense()
    probe_env.scan.write_text(probe_env.scan.read_text().replace("/dev/ada0", "/dev/nda1").replace("atacam", "nvme"))
    names = [f.name for f in parse_envelope(probe_env.run_gate("drivecanary-collect").stdout).frames]
    assert "smartctl.dev:/dev/nvme1:nvme" in names and not any("nda1" in n for n in names)


def test_agent_reports_from_a_freebsd_host(
    cfg: Config,
    factory: sessionmaker[Session],
    probe_env: ProbeEnv,
    hub: str,  # noqa: F811
) -> None:
    probe_env.become_opnsense()
    token = _push_host(factory, "opnsense")
    cp = probe_env.run_agent(hub, token)
    assert cp.returncode == 0, cp.stderr
    assert "runs=1 pools=0 attrlog_lines=0" in cp.stdout
    assert (probe_env.agent_state / "offsets").read_text() == "", "no smartd there: nothing to keep a place in"
    host = _host(factory, "opnsense")
    assert host.state == HostState.OK.value
    assert host.os_release == "OPNsense 25.7.10_10 (amd64), FreeBSD 14.2-RELEASE-p11"
    assert host.machine_id == "000000000000000000000000c0ffee00" and host.smartctl_version == "7.4"
    with factory() as s:
        drive = s.scalar(select(Drive))
        assert drive is not None and drive.device_type == "sat" and drive.kind == "ssd"
        sighting = s.scalar(select(DriveSighting))
        assert sighting is not None and sighting.dev_name == "/dev/ada0" and sighting.current
        assert s.scalar(select(func.count(SmartRun.id))) == 1
        assert s.scalar(select(func.count(Pool.id))) == 0

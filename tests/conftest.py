"""Test fixtures. Everything runs on a throwaway SQLite file under tmp_path; nothing touches the network."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from drivecanary.config import Config
from drivecanary.db import make_engine, sessionmaker_for
from drivecanary.models import Base

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
HOST_DIR = ROOT / "deploy" / "host"


def load_json(name: str) -> dict[str, Any]:
    with (FIXTURES / "smartctl" / name).open() as fh:
        return json.load(fh)  # type: ignore[no-any-return]


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config()
    c.paths.state_dir = tmp_path / "state"
    return c


@pytest.fixture
def engine(cfg: Config) -> Iterator[sa.Engine]:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    e = make_engine(cfg.db_path)
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture
def factory(engine: sa.Engine) -> sessionmaker[Session]:
    return sessionmaker_for(engine)


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    s = factory()
    try:
        yield s
    finally:
        s.close()


# --------------------------------------------------------------------------- the real gate and probe, shimmed

SCAN_JSON = """{
  "json_format_version": [1, 0],
  "smartctl": {"version": [7, 4], "exit_status": 0},
  "devices": [
    {
      "name": "/dev/sda",
      "info_name": "/dev/sda [SAT]",
      "type": "sat",
      "protocol": "ATA"
    },
    {
      "name": "/dev/sdb",
      "info_name": "/dev/sdb [SAT]",
      "type": "sat",
      "protocol": "ATA"
    },
    {
      "name": "/dev/nvme0",
      "info_name": "/dev/nvme0",
      "type": "nvme",
      "protocol": "NVMe"
    },
    {
      "name": "/dev/sr0",
      "info_name": "/dev/sr0",
      "type": "scsi",
      "protocol": "SCSI",
      "open_error": "Unable to open"
    }
  ]
}
"""

#: what `smartctl --scan-open -j` printed on the router: one device, and it is the last thing in the list
FREEBSD_SCAN_JSON = """{
  "json_format_version": [
    1,
    0
  ],
  "smartctl": {
    "version": [
      7,
      5
    ],
    "platform_info": "FreeBSD 14.2-RELEASE-p11 amd64",
    "argv": [
      "smartctl",
      "--scan-open",
      "-j"
    ],
    "exit_status": 0
  },
  "devices": [
    {
      "name": "/dev/ada0",
      "info_name": "/dev/ada0",
      "type": "atacam",
      "protocol": "ATA"
    }
  ]
}
"""

ZPOOL_LIST = (
    "tank\tONLINE\t8001563222016\t3000000000000\t5001563222016\t4\t37\n"
    "backup\tDEGRADED\t4000000000000\t1000000000000\t3000000000000\t1\t25\n"
)
ZPOOL_STATUS = """  pool: tank
 state: ONLINE
  scan: scrub repaired 0B in 05:12:33 with 0 errors on Sun Sep 14 05:12:35 2026
config:

\tNAME        STATE     READ WRITE CKSUM
\ttank        ONLINE       0     0     0
\t  mirror-0  ONLINE       0     0     0
\t    sda     ONLINE       0     0     0
\t    sdb     ONLINE       0     0     0

errors: No known data errors

  pool: backup
 state: DEGRADED
status: One or more devices has been removed by the administrator.
  scan: none requested
config:

\tNAME        STATE     READ WRITE CKSUM
\tbackup      DEGRADED     0     0     0
\t  mirror-0  DEGRADED     0     0     0
\t    sdc     ONLINE       0     0     7
\t    sdd     REMOVED      0     0     0

errors: No known data errors
"""
BTRFS_STATS = """[/dev/sde].write_io_errs    3
[/dev/sde].read_io_errs     0
[/dev/sde].flush_io_errs    0
[/dev/sde].corruption_errs  0
[/dev/sde].generation_errs  0
"""


BTRFS_SCRUB = """UUID:             1234-uuid
Scrub started:    Sun Sep 14 03:00:00 2026
Status:           finished
Duration:         5:12:33
Total to scrub:   10.24TiB
Rate:             558.12MiB/s
Error summary:    no errors found
"""


#: the block devices behind the fixture host: the serials are the smartctl fixtures', sdc to sde report nothing
LSBLK_JSON = json.dumps(
    {
        "blockdevices": [
            {"kname": "sda", "path": "/dev/sda", "type": "disk", "serial": "9RK1XXXX", "pkname": None},
            {"kname": "sda1", "path": "/dev/sda1", "type": "part", "serial": None, "pkname": "sda"},
            {"kname": "sdb", "path": "/dev/sdb", "type": "disk", "serial": "MSK423Y20S3HBC", "pkname": None},
            {"kname": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk", "serial": "BTNH93710FS91P0B", "pkname": None},
            {"kname": "nvme0n1p2", "path": "/dev/nvme0n1p2", "type": "part", "serial": None, "pkname": "nvme0n1"},
            {"kname": "dm-0", "path": "/dev/mapper/vault", "type": "crypt", "serial": None, "pkname": "nvme0n1p2"},
            {"kname": "sdc", "path": "/dev/sdc", "type": "disk", "serial": "NOSMARTHERE1", "pkname": None},
        ]
    },
    indent=1,
)


def _shim(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@dataclass
class ProbeEnv:
    gate: Path
    probe: Path
    shims: Path
    attrlog_dir: Path
    devices_conf: Path
    agent: Path
    agent_conf: Path
    agent_token: Path
    agent_state: Path
    scan: Path
    opnsense_config: Path

    def become_opnsense(self) -> None:
        """Shims that make the scripts take their FreeBSD paths: what `uname`, `sysctl`, `id` and the rest
        answered on a real OPNsense 25.7 router (2026-09-28), one SATA SSD, UFS, no smartd."""
        _shim(
            self.shims / "uname",
            'case "$1" in -s) echo FreeBSD ;; -r) echo 14.2-RELEASE-p11 ;; *) echo FreeBSD ;; esac\n',
        )
        _shim(self.shims / "id", 'case "$1" in -u) echo 0 ;; *) echo "uid=0(root) gid=0(wheel)" ;; esac\n')
        _shim(self.shims / "sudo", 'echo "sudo must not be used when already root" >&2; exit 1\n')
        _shim(
            self.shims / "sysctl",
            'case "$2" in\n'
            "  kern.hostuuid) echo 00000000-0000-0000-0000-0000c0ffee00 ;;\n"
            '  kern.boottime) echo "{ sec = 1789678450, usec = 19379 } Thu Sep 17 16:54:10 2026" ;;\n'
            "  kern.disks) echo ada0 ;;\n"
            "esac\n",
        )
        _shim(self.shims / "opnsense-version", 'echo "OPNsense 25.7.10_10 (amd64)"\n')
        _shim(self.shims / "freebsd-version", "echo 14.2-RELEASE-p11\n")
        _shim(self.shims / "geom", 'echo "Geom name: ada0"; echo "   descr: SAMSUNG SSD PM830 mSATA 32GB"\n')
        for absent in ("zpool", "btrfs", "findmnt", "lsblk", "mdadm"):
            _shim(self.shims / absent, f'echo "{absent}: must not be run on FreeBSD here" >&2; exit 99\n')
        (self.shims / "zpool").unlink()  # present on FreeBSD, but this router has no pools
        _shim(self.shims / "zpool", 'case "$1" in list) exit 0 ;; status) echo "no pools available"; exit 0 ;; esac\n')
        self.scan.write_text(FREEBSD_SCAN_JSON)
        self.opnsense_config.write_text(
            "<opnsense>\n  <system>\n    <timezone>Europe/Amsterdam</timezone>\n  </system>\n</opnsense>\n"
        )

    def run_agent(self, hub_url: str, token: str) -> subprocess.CompletedProcess[str]:
        """The real agent, as its timer would run it, against a hub at hub_url."""
        self.agent_conf.write_text(f"HUB_URL={hub_url}\n")
        self.agent_token.write_text(f"Authorization: Bearer {token}\n")
        self.agent_state.mkdir(exist_ok=True)
        env = dict(os.environ, STATE_DIRECTORY=str(self.agent_state), PATH=f"{self.shims}:{os.environ['PATH']}")
        env.pop("SSH_ORIGINAL_COMMAND", None)
        return subprocess.run(
            ["sh", str(self.agent)], env=env, capture_output=True, text=True, timeout=120, check=False
        )

    def run_gate(self, command: str) -> subprocess.CompletedProcess[bytes]:
        env = dict(os.environ, SSH_ORIGINAL_COMMAND=command, PATH=f"{self.shims}:{os.environ['PATH']}")
        return subprocess.run(["sh", str(self.gate)], env=env, capture_output=True, timeout=120, check=False)


@pytest.fixture
def probe_env(tmp_path: Path) -> ProbeEnv:
    """Copies of the real gate and probe with their fixed paths pointed into tmp_path, and shims for every
    command the probe runs: smartctl answers from the JSON fixtures, sudo passes through, zpool/btrfs/findmnt
    print canned state."""
    shims = tmp_path / "shims"
    shims.mkdir()
    fx = FIXTURES / "smartctl"
    attrlog_dir = tmp_path / "smartmontools"
    attrlog_dir.mkdir()
    shutil.copy(FIXTURES / "attrlog" / "atlas" / "attrlog.ST20000NM007D_3DJ103-ZXA00001.ata.csv", attrlog_dir)
    devices_conf = tmp_path / "devices.conf"
    scan = tmp_path / "scan.json"
    scan.write_text(SCAN_JSON)
    probe = tmp_path / "probe"
    gate = tmp_path / "gate"
    path_line = f"PATH={shims}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    text = (HOST_DIR / "probe").read_text()
    text = text.replace("PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", path_line, 1)
    text = text.replace("DEVICES_CONF=/etc/drivecanary/devices.conf", f"DEVICES_CONF={devices_conf}", 1)
    # a lock of this test's own: the real one is system-wide, and one test's probe must not turn away another's
    text = text.replace("LOCKDIR=/run/lock", f"LOCKDIR={tmp_path}", 1)
    text = text.replace("LOCKDIR=/var/run", f"LOCKDIR={tmp_path}", 1)
    text = text.replace("DEVICES_CONF=/usr/local/etc/drivecanary/devices.conf", f"DEVICES_CONF={devices_conf}", 1)
    probe.write_text(text)
    text = (HOST_DIR / "gate").read_text()
    text = text.replace("PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", path_line, 1)
    text = text.replace("PROBE=/usr/local/lib/drivecanary/probe", f"PROBE={probe}", 1)
    text = text.replace("ATTRLOG_DIR=/var/lib/smartmontools", f"ATTRLOG_DIR={attrlog_dir}", 1)
    text = text.replace("ATTRLOG_DIR=/var/db/smartmontools", f"ATTRLOG_DIR={tmp_path}/no-smartd-here", 1)
    text = text.replace("/conf/config.xml", f"{tmp_path}/config.xml", 1)
    text = text.replace("/var/db/zoneinfo", f"{tmp_path}/zoneinfo", 1)
    gate.write_text(text)
    agent = tmp_path / "agent"
    text = (HOST_DIR / "agent").read_text()
    text = text.replace("PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", path_line, 1)
    text = text.replace("GATE=/usr/local/lib/drivecanary/gate", f"GATE={gate}", 1)
    text = text.replace("CONF=/etc/drivecanary/agent.conf", f"CONF={tmp_path}/agent.conf", 1)
    text = text.replace("TOKEN=/etc/drivecanary/agent.token", f"TOKEN={tmp_path}/agent.token", 1)
    text = text.replace("CONF=/usr/local/etc/drivecanary/agent.conf", f"CONF={tmp_path}/agent.conf", 1)
    text = text.replace("TOKEN=/usr/local/etc/drivecanary/agent.token", f"TOKEN={tmp_path}/agent.token", 1)
    text = text.replace("STATE_DEFAULT=/var/db/drivecanary-agent", f"STATE_DEFAULT={tmp_path}/agent-state", 1)
    agent.write_text(text)
    for script in (probe, gate, agent):  # the gate runs the probe directly, as sudo would
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    _shim(shims / "sudo", 'while [ "$1" = -n ]; do shift; done\nexec "$@"\n')
    _shim(
        shims / "smartctl",
        f"""case "$1" in
  --version) echo "smartctl 7.4 2023-08-01 r5530 [x86_64-linux-6.1.0] (local build)"; exit 0 ;;
  --scan-open) cat "{scan}"; exit 0 ;;
esac
for last; do :; done
case "$last" in
  /dev/sda) cat "{fx}/smart-ata.json"; exit 0 ;;
  /dev/sdb) cat "{fx}/smart-fail2.json"; exit 216 ;;
  /dev/nvme0) cat "{fx}/smart-nvme.json"; exit 0 ;;
  /dev/sdz) cat "{fx}/smart-scsi.json"; exit 0 ;;
  /dev/ada0|/dev/nvme1) cat "{fx}/smart-ata-full.json"; exit 0 ;;
esac
echo '{{"smartctl": {{"exit_status": 2, "messages": [{{"string": "no such device", "severity": "error"}}]}}}}'
exit 2
""",
    )
    _shim(
        shims / "zpool",
        f"""case "$1 $2" in
  "list -Hp") printf '%s' '{ZPOOL_LIST.replace(chr(10), "\\n")}' | sed 's/\\\\n/\\n/g'; exit 0 ;;
  "status -j") echo "invalid option 'j'" >&2; exit 2 ;;
  "status -p") cat "{tmp_path}/zpool.status"; exit 0 ;;
esac
exit 1
""",
    )
    (tmp_path / "zpool.status").write_text(ZPOOL_STATUS)
    _shim(shims / "findmnt", 'echo "/mnt/btr 1234-uuid"\n')
    (tmp_path / "btrfs.stats").write_text(BTRFS_STATS)
    (tmp_path / "btrfs.scrub").write_text(BTRFS_SCRUB)
    _shim(
        shims / "btrfs",
        f"""case "$1 $2" in
  "device stats") cat "{tmp_path}/btrfs.stats"; exit 0 ;;
  "scrub status") cat "{tmp_path}/btrfs.scrub"; exit 0 ;;
esac
exit 1
""",
    )
    (tmp_path / "lsblk.json").write_text(LSBLK_JSON)
    _shim(shims / "lsblk", f'cat "{tmp_path}/lsblk.json"\n')
    _shim(shims / "mdadm", 'echo "MD_LEVEL=raid1"\n')
    return ProbeEnv(
        gate=gate,
        probe=probe,
        shims=shims,
        attrlog_dir=attrlog_dir,
        devices_conf=devices_conf,
        agent=agent,
        agent_conf=tmp_path / "agent.conf",
        agent_token=tmp_path / "agent.token",
        agent_state=tmp_path / "agent-state",
        scan=scan,
        opnsense_config=tmp_path / "config.xml",
    )

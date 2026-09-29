"""The units say what the CLI can do, every timer has its service, every script answers help."""

from __future__ import annotations

import configparser
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from drivecanary.cli import app

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
UNITS = DEPLOY / "systemd"


def _parse(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read(path)
    return cp


def test_every_timer_has_its_service_and_every_exec_is_a_real_command() -> None:
    files = sorted(UNITS.iterdir())
    services = {f.name for f in files if f.suffix == ".service"}
    runner = CliRunner()
    for f in files:
        cp = _parse(f)
        assert cp["Unit"]["Description"].startswith("drivecanary:"), f.name
        if f.suffix == ".timer":
            assert cp["Timer"]["Unit"] in services, f.name
            assert cp["Timer"].get("OnCalendar"), f.name
            assert cp["Timer"].get("Persistent") == "true", f.name
        else:
            exec_ = cp["Service"]["ExecStart"]
            if "drivecanary " in exec_:
                words = exec_.split("drivecanary ", 1)[1].split()
                result = runner.invoke(app, [*words, "--help"])
                assert result.exit_code == 0, f"{f.name}: drivecanary {' '.join(words)} -> {result.output[:200]}"
            else:
                assert Path(exec_).name == "backup.sh", f.name
            assert cp["Service"].get("User") in ("drivecanary", "drivecanary-web"), f.name
            assert cp["Service"].get("UMask") == "0007", f"{f.name}: the database files must stay group-writable"


def test_oneshots_have_a_start_timeout_and_survive_a_stop() -> None:
    """A oneshot has no start timeout by default and a wedged run swallows every later timer firing; a job
    stopped on request is not a failed job."""
    jobs = [f for f in UNITS.glob("*.service") if "Type=oneshot" in f.read_text()]
    assert len(jobs) == 2
    for f in jobs:
        text = f.read_text()
        assert "TimeoutStartSec=" in text and "SuccessExitStatus=" in text and "SIGTERM" in text, f.name
        assert "Nice=10" in text and "CPUWeight=20" in text, f.name


def test_collect_unit_does_not_fail_when_a_host_is_down() -> None:
    text = (UNITS / "drivecanary-collect.service").read_text()
    assert "SuccessExitStatus=1 SIGTERM" in text


def test_web_cannot_read_the_collectors_key() -> None:
    text = (UNITS / "drivecanary-web.service").read_text()
    assert "User=drivecanary-web" in text and "InaccessiblePaths=-/var/lib/drivecanary/ssh" in text


def test_every_script_answers_help_dash_dash_help_and_dash_h_alike() -> None:
    scripts = [DEPLOY / n for n in ("install.sh", "update.sh", "backup.sh", "drivecanary-cli")]
    scripts += [DEPLOY / "host" / n for n in ("install-host.sh", "probe", "gate", "agent")]
    for script in scripts:
        for word in ("help", "--help", "-h"):
            cp = subprocess.run(["sh", str(script), word], capture_output=True, text=True, timeout=30, check=False)
            assert cp.returncode == 0, (script.name, word, cp.stderr[:200])
            assert "usage" in cp.stdout.lower(), (script.name, word, cp.stdout[:200])


def test_host_side_files_are_what_the_design_says() -> None:
    sudoers = (DEPLOY / "host" / "sudoers").read_text()
    assert 'drivecanary ALL=(root) NOPASSWD: /usr/local/lib/drivecanary/probe ""' in sudoers
    assert "smartctl" not in sudoers.split("\n\n")[-1] or "Never" in sudoers
    install = (DEPLOY / "host" / "install-host.sh").read_text()
    assert 'restrict,from="%s",command="/usr/local/lib/drivecanary/gate"' in install
    assert "usermod -p '*' drivecanary" in install and "--shell /bin/sh" in install
    assert "visudo -c" in install and "/etc/sudoers.d/drivecanary" in install
    gate = (DEPLOY / "host" / "gate").read_text()
    assert "set -f" in gate and 'sudo -n "$PROBE"' in gate
    probe = (DEPLOY / "host" / "probe").read_text()
    assert "flock -n" in probe and "timeout -k 5" in probe and "standby,$STANDBY_EXIT" in probe
    for f in ("probe", "gate", "agent", "install-host.sh"):
        assert (DEPLOY / "host" / f).stat().st_mode & 0o111, f


def test_the_push_agent_is_what_the_design_says() -> None:
    host = DEPLOY / "host"
    agent = (host / "agent").read_text()
    assert '-H @"$TOKEN"' in agent, "the token never appears on a command line"
    assert "--connect-timeout" in agent and "--max-time" in agent and "flock -n" in agent
    assert "SPOOL_MAX_FILES" in agent and "SPOOL_MAX_MB" in agent
    code = [line for line in agent.splitlines() if not line.lstrip().startswith("#")]
    assert not any("sudo" in line for line in code), "only the gate runs the probe"
    unit = (host / "drivecanary-agent.service").read_text()
    assert "User=drivecanary" in unit and "TimeoutStartSec=" in unit and "StateDirectory=drivecanary-agent" in unit
    active = [line for line in unit.splitlines() if not line.startswith("#")]
    assert not any("NoNewPrivileges" in line or "PrivateDevices" in line for line in active), "sudo and the disks"
    timer = (host / "drivecanary-agent.timer").read_text()
    assert "OnBootSec=" in timer and "Persistent=true" in timer and "OnCalendar=hourly" in timer
    install = (host / "install-host.sh").read_text()
    assert '-m 0600 "$T/agent.token" /etc/drivecanary/agent.token' in install
    assert 'rm -f "$HOME_DIR/.ssh/authorized_keys"' in install, "a push host holds no key of the hub's"


def test_the_hub_runs_the_ingest_listener_as_the_collector() -> None:
    ingest = (UNITS / "drivecanary-ingest.service").read_text()
    assert "User=drivecanary\n" in ingest and "drivecanary ingest serve" in ingest
    for name in ("install.sh", "update.sh"):
        assert "drivecanary-ingest.service" in (DEPLOY / name).read_text(), name


#: each host script's VERSION, and the sha256 of the file that carries it
RELEASED = {
    "probe": (3, "029cc921d34c1cb472174ee1245a3d348af0364bcf7bcdacc1f0c4d1b5cfd092"),
    "gate": (2, "a0348ea47356fccd17de7bde1f5c85d0badcc85bd1fa9a497daf21d44f1bc5c9"),
    "agent": (2, "9406824c0b16a3b76b37162ac03136657e43af5c89a8f22bcb032b05c22d12d8"),
}


def test_a_changed_host_script_has_a_new_version() -> None:
    """A host's page says which version of each script it runs, and that is only worth something if a
    changed script never keeps its number: the probe was fixed twice as v1, and a host that had the fix
    looked the same as one that did not (2026-09-28)."""
    import hashlib
    import re

    for name, (version, digest) in RELEASED.items():
        text = (DEPLOY / "host" / name).read_bytes()
        found = re.search(rb"^VERSION=(\d+)$", text, re.M)
        assert found is not None, name
        now = hashlib.sha256(text).hexdigest()
        if now != digest:
            assert int(found.group(1)) > version, (
                f"deploy/host/{name} has changed and is still VERSION={version}: raise it, then record "
                f'"{name}": ({version + 1}, "<the new sha256>") in RELEASED'
            )
            raise AssertionError(
                f'deploy/host/{name} is a new version: record "{name}": ({int(found.group(1))}, "{now}")'
            )
        assert int(found.group(1)) == version, name


def test_the_installer_knows_opnsense() -> None:
    install = (DEPLOY / "host" / "install-host.sh").read_text()
    assert "/usr/local/etc/cron.d/drivecanary" in install and "logger -t drivecanary-agent" in install
    assert "only --push is supported here" in install
    freebsd = install[install.index('if [ "$(uname -s)" = FreeBSD ]; then') : install.index("  exit 0\nfi\n")]
    assert "adduser" not in freebsd and "sudoers" not in freebsd and "authorized_keys" not in freebsd
    assert "-g root" not in freebsd, "gid 0 is wheel there: by number"
    assert 'ssh "$TARGET" "sh -c ' in install, "the far login shell need not be a Bourne shell"
    for script in ("probe", "gate", "agent"):
        text = (DEPLOY / "host" / script).read_text()
        assert "stat -c" not in text and "/proc/sys/kernel/random/uuid" not in text, script


def test_the_cli_wrapper_and_update_are_installed_the_safe_way() -> None:
    wrapper = (DEPLOY / "drivecanary-cli").read_text()
    assert "sudo -u drivecanary" in wrapper and "update) exec sudo /usr/local/sbin/drivecanary-update" in wrapper
    sudoers = (DEPLOY / "sudoers.example").read_text()
    assert "NOPASSWD: /usr/local/sbin/drivecanary-update" in sudoers and "deploy/update.sh" not in [
        line for line in sudoers.splitlines() if line.startswith("%sudo")
    ]
    for name in ("install.sh", "update.sh"):
        text = (DEPLOY / name).read_text()
        assert 'deploy/update.sh" /usr/local/sbin/drivecanary-update' in text, name
        assert "/usr/local/bin/uv" in text and '"$UV" sync' in text, name
    update = (DEPLOY / "update.sh").read_text()
    pull = update.index('git -C "$APP" pull')
    reexec = update.index('DRIVECANARY_UPDATE_STAGE2=1 exec "$APP/deploy/update.sh"')
    assert pull < reexec < update.index("sync --frozen")
    assert 'echo "update: pulled $was -> $now"' in update, "one command, and it says what it brought"
    assert (
        update.index("systemctl stop 'drivecanary-*.timer'")
        < update.index("db migrate")
        < update.index('systemctl enable --now "$(basename "$t")"')
    )


def test_backup_snapshots_instead_of_copying() -> None:
    text = (DEPLOY / "backup.sh").read_text()
    assert '"$DC" db snapshot' in text and "cp " not in text.split("db snapshot")[0]
    assert "BACKUP_COPY_DIR" in text and "prune_rolling" in text

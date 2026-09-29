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

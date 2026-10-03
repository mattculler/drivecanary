"""install-host.sh from the workstation's side: what it sends over ssh, and what it keeps for the next time."""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.conftest import HOST_DIR, _shim

INSTALL = HOST_DIR / "install-host.sh"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGVbf5nkgYQxB9fXn2w3jOsyB9m0pSvFi0GX5XvTx8aZ drivecanary-hub@hub"


class Workstation:
    """A workstation whose ssh goes nowhere: it keeps what it was told to send."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.kept = root / "kept"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.calls = root / "ssh-calls"
        _shim(self.bin / "ssh", f'echo "$*" >> "{self.calls}"\ncat > "{root}/sent-$(wc -l < "{self.calls}").tar"\n')

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", DRIVECANARY_HOSTS_DIR=str(self.kept))
        return subprocess.run([str(INSTALL), *args], env=env, capture_output=True, text=True, check=False)

    def sent(self, n: int = 1) -> dict[str, str]:
        with tarfile.open(self.root / f"sent-{n}.tar") as tar:
            return {
                m.name.removeprefix("./"): tar.extractfile(m).read().decode()  # type: ignore[union-attr]
                for m in tar.getmembers()
                if m.isfile()
            }

    def targets(self) -> list[str]:
        return [line.split()[0] for line in self.calls.read_text().splitlines()]


@pytest.fixture
def ws(tmp_path: Path) -> Workstation:
    return Workstation(tmp_path)


def test_a_pull_install_is_kept_and_runs_again_from_what_was_kept(ws: Workstation) -> None:
    keyfile = ws.root / "id_ed25519.pub"
    keyfile.write_text(KEY + "\n")
    cp = ws.run("atlas", "--hub-ip", "10.100.100.84", "--hub-key", str(keyfile), "--target", "root@atlas.domain")
    assert cp.returncode == 0, cp.stderr
    sent = ws.sent()
    assert sent["mode"] == "pull\n" and sent["selftests.do"] == "\n" and "agent" not in sent
    assert sent["authorized_keys"] == f'restrict,from="10.100.100.84",command="/usr/local/lib/drivecanary/gate" {KEY}\n'
    kept = ws.kept / "atlas"
    assert kept.stat().st_mode & 0o777 == 0o600 and ws.kept.stat().st_mode & 0o777 == 0o700
    assert kept.read_text().splitlines()[1:] == [
        "MODE=pull",
        "TARGET=root@atlas.domain",
        "HUB_IP=10.100.100.84",
        f"HUB_KEY={KEY}",
    ], "the key's text, not the file's name: the file may be gone next time"
    assert "next time, " in cp.stdout and "install-host.sh atlas" in cp.stdout

    cp = ws.run("atlas")
    assert cp.returncode == 0, cp.stderr
    assert "as last time" in cp.stdout and ws.targets() == ["root@atlas.domain"] * 2
    assert ws.sent(2) == sent, "the same install, from nothing but the name"


def test_a_push_install_keeps_the_token_and_takes_new_options_over_old(ws: Workstation) -> None:
    cp = ws.run("pve", "--push", "--hub-url", "http://10.100.100.84:8081/", "--token", "tok_abc-123", "--self-tests")
    assert cp.returncode == 0, cp.stderr
    sent = ws.sent()
    assert sent["mode"] == "push\n" and sent["agent.conf"] == "HUB_URL=http://10.100.100.84:8081\n"
    assert sent["agent.token"] == "Authorization: Bearer tok_abc-123\n" and sent["selftests.do"] == "apply\n"
    assert (ws.kept / "pve").read_text().splitlines()[1:] == [
        "MODE=push",
        "TARGET=pve",
        "HUB_URL=http://10.100.100.84:8081",
        "TOKEN=tok_abc-123",
        "SELFTESTS=apply",
    ]
    assert ws.run("pve").returncode == 0 and ws.sent(2)["selftests.do"] == "apply\n", "the schedule is applied again"
    cp = ws.run("pve", "--token", "tok_new", "--no-self-tests")
    assert cp.returncode == 0 and "as last time" not in cp.stdout
    assert ws.sent(3)["agent.token"] == "Authorization: Bearer tok_new\n" and ws.sent(3)["selftests.do"] == "remove\n"
    assert "SELFTESTS" not in (ws.kept / "pve").read_text(), "removed once is removed"
    assert ws.run("pve").returncode == 0 and ws.sent(4)["selftests.do"] == "\n"
    assert ws.sent(4)["agent.token"] == "Authorization: Bearer tok_new\n"


def test_moving_a_host_the_other_way_needs_that_ways_options(ws: Workstation) -> None:
    assert ws.run("pve", "--push", "--hub-url", "http://hub:8081", "--token", "tok").returncode == 0
    cp = ws.run("pve", "--pull")
    assert cp.returncode == 64 and "usage:" in cp.stderr and not (ws.root / "sent-2.tar").exists()
    assert "MODE=push" in (ws.kept / "pve").read_text(), "nothing kept from a run that sent nothing"
    cp = ws.run("pve", "--pull", "--hub-ip", "10.0.0.1", "--hub-key", KEY)
    assert cp.returncode == 0, cp.stderr
    assert ws.sent(2)["mode"] == "pull\n" and "TOKEN" not in (ws.kept / "pve").read_text()


def test_all_does_every_kept_host_and_says_which_failed(ws: Workstation) -> None:
    cp = ws.run("--all")
    assert cp.returncode == 1 and "no host has been installed from here yet" in cp.stderr
    assert ws.run("a", "--hub-ip", "10.0.0.1", "--hub-key", KEY).returncode == 0
    assert ws.run("b", "--push", "--hub-url", "http://hub:8081", "--token", "tok", "--target", "root@b").returncode == 0
    cp = ws.run("--all")
    assert cp.returncode == 0, cp.stderr
    assert ws.targets() == ["a", "root@b", "a", "root@b"] and "all 2 hosts done: a b" in cp.stdout
    _shim(ws.bin / "ssh", 'case "$1" in a) exit 255 ;; esac\ncat > /dev/null\n')
    cp = ws.run("--all")
    assert cp.returncode == 1 and "FAILED: a" in cp.stderr
    assert ws.run("a", "--forget").returncode == 0 and not (ws.kept / "a").exists()
    assert ws.run("--all").returncode == 0


def test_what_is_not_a_host_name_is_refused(ws: Workstation) -> None:
    for bad in ("--push", "../x", ".hidden", "a/b"):
        cp = ws.run(bad, "--hub-ip", "1.2.3.4", "--hub-key", KEY)
        assert cp.returncode == 64, bad
    assert not ws.kept.exists()
    cp = ws.run("--all", "extra")
    assert cp.returncode == 64
    assert ws.run("nobody").returncode == 64, "nothing kept, nothing given"

"""scripts/check-private: what is yours stays out of commits, by a list that is never committed itself."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-private"


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.list = root / "private-patterns"
        self.list.write_text("# mine\n\\bhomebox\\b\n10\\.9\\.8\\.\nSERIAL123\n\n")
        self.dir = root / "repo"
        self.dir.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.dir, check=True, capture_output=True, text=True).stdout

    def check(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        e = {**os.environ, "DRIVECANARY_PRIVATE_PATTERNS": str(self.list), **(env or {})}
        return subprocess.run([str(SCRIPT), *args], cwd=self.dir, env=e, capture_output=True, text=True, check=False)

    def stage(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text)
        self.git("add", name)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def test_a_staged_line_of_yours_is_refused(repo: Repo) -> None:
    repo.stage("ok.txt", "host atlas at 10.100.100.5\n")
    assert repo.check().returncode == 0
    repo.stage("bad.txt", "ssh root@homebox\nfine\n")
    cp = repo.check()
    assert cp.returncode == 1 and "+ssh root@homebox" in cp.stderr and "fine" not in cp.stderr
    assert "homeboxes" not in cp.stderr


def test_a_file_named_for_something_of_yours_is_refused(repo: Repo) -> None:
    repo.stage("attrlog.MODEL-SERIAL123.csv", "nothing here\n")
    cp = repo.check()
    assert cp.returncode == 1 and "attrlog.MODEL-SERIAL123.csv" in cp.stderr


def test_a_line_taken_out_is_not_a_line_added(repo: Repo) -> None:
    repo.stage("f.txt", "homebox\n")
    repo.git("commit", "-q", "--no-verify", "-m", "before")
    repo.stage("f.txt", "atlas\n")
    assert repo.check().returncode == 0


def test_a_commit_message(repo: Repo, tmp_path: Path) -> None:
    msg = tmp_path / "msg"
    msg.write_text("Fix the probe on homebox\n\n# Please enter the commit message (homebox in a comment is git's)\n")
    cp = repo.check("--message", str(msg))
    assert cp.returncode == 1 and "Fix the probe on homebox" in cp.stderr and "Please enter" not in cp.stderr
    msg.write_text("Fix the probe\n")
    assert repo.check("--message", str(msg)).returncode == 0


def test_the_whole_history(repo: Repo) -> None:
    repo.stage("a.txt", "clean\n")
    repo.git("commit", "-q", "-m", "one")
    cp = repo.check("--history")
    assert cp.returncode == 0 and "nothing of yours in 1 commits" in cp.stdout
    repo.stage("a.txt", "at 10.9.8.7\n")
    repo.git("commit", "-q", "--no-verify", "-m", "two, from SERIAL123")
    repo.stage("a.txt", "clean again\n")
    repo.git("commit", "-q", "--no-verify", "-m", "three")
    cp = repo.check("--history")
    assert cp.returncode == 1, "taken out again later, it is still in the history"
    assert "a.txt:1:at 10.9.8.7" in cp.stderr and "two, from SERIAL123" in cp.stderr


def test_no_list_checks_nothing_and_says_so(repo: Repo) -> None:
    repo.stage("bad.txt", "homebox\n")
    cp = repo.check(env={"DRIVECANARY_PRIVATE_PATTERNS": str(repo.root / "absent")})
    assert cp.returncode == 0 and "nothing is checked" in cp.stderr

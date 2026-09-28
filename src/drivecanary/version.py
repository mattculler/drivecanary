"""The running version: the git commit this checkout is at, not a number nobody bumps.

`0.1.0` in pyproject is what packaging needs; what the UI, `--version` and the about page want is which
commit is deployed and when it was made. A checkout answers that from git; an installed wheel falls
back to the package metadata.
"""

from __future__ import annotations

import subprocess
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str | None:
    if not (REPO / ".git").exists():
        return None
    try:
        # safe.directory: the VM checkout is owned by the service user and read by others (root, the admin)
        r = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(REPO), *args], capture_output=True, text=True, timeout=3
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def describe() -> str:
    """'a1b2c3d 2026-09-28', with ' +local' when the tree has uncommitted changes."""
    head = _git("log", "-1", "--format=%h %cs")
    if not head:
        try:
            return metadata.version("drivecanary")
        except metadata.PackageNotFoundError:
            return "unknown"
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    return head + (" +local" if dirty else "")


def details() -> dict[str, str | None]:
    """For the about page: hash, date, subject, branch."""
    return {
        "commit": _git("rev-parse", "HEAD"),
        "short": _git("rev-parse", "--short=12", "HEAD"),
        "date": _git("log", "-1", "--format=%cI"),
        "subject": _git("log", "-1", "--format=%s"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": "yes" if _git("status", "--porcelain", "--untracked-files=no") else "no",
    }

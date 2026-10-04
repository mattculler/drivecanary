"""What changes with a host's state beyond the field itself."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from drivecanary.models import DriveSighting, Host, HostState, Pool


def retire(session: Session, host: Host) -> tuple[int, int]:
    """Retire a host, and with it what was last seen on it: its drives (unless another host has one now) and
    its pools. A retired host is not collected from, so nothing of it would ever be current again; left as it
    was, its drives would sit on the front page going stale. Any of them comes back the first time a host
    reports it. Returns (drives, pools) retired."""
    host.state = HostState.RETIRED.value
    drives = 0
    for s in session.scalars(select(DriveSighting).where(DriveSighting.host_id == host.id, DriveSighting.current)):
        s.current = False
        elsewhere = session.scalar(
            select(DriveSighting.id).where(
                DriveSighting.drive_id == s.drive_id, DriveSighting.current, DriveSighting.host_id != host.id
            )
        )
        if elsewhere is None and not s.drive.retired:
            s.drive.retired = True
            drives += 1
    pools = 0
    for p in session.scalars(select(Pool).where(Pool.host_id == host.id, ~Pool.retired)):
        p.retired = True
        pools += 1
    return drives, pools

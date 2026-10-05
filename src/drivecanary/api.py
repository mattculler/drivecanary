"""The JSON API: what the page says about a host, for another service to ask.

    GET /api/v1/hosts          every host but the retired ones, each as below without its drives and pools
    GET /api/v1/host/NAME      one host: ok, status, problems, its drives and its pools

`ok` is the one field a monitor needs: true when nothing about the host, its drives or its pools is failing,
warning, unreadable, stale or unknown (a drive asleep in standby is fine). `status` says how bad, in the
page's words; `problems` says what, one line each. The verdicts are the page's own, from the same queries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from drivecanary.models import HostState, Verdict
from drivecanary.queries import DriveRow, HostRow, PoolRow, rank
from drivecanary.timeutil import spelled

VERSION = 1
#: verdicts that are nothing to worry about
FINE = (Verdict.OK, Verdict.SKIPPED)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt is not None else None


def _ago(hours: float | None) -> str:
    if hours is None:
        return "never"
    return f"{spelled(hours) or f'{hours:.1f} hours'} ago"


def drive_json(d: DriveRow) -> dict[str, Any]:
    r = d.latest
    return {
        "id": d.drive.id,
        "dev": d.dev_name,
        "model": d.drive.model or d.drive.model_key,
        "serial": d.drive.serial or d.drive.serial_key,
        "kind": d.drive.kind,
        "capacity_bytes": d.drive.capacity_bytes,
        "verdict": d.verdict.value,
        "reasons": d.reasons,
        "temperature_c": r.temp_c if r else None,
        "power_on_hours": r.power_on_hours if r else None,
        "read_at": iso(r.collected_at) if r else None,
        "self_test_percent": d.testing,
        "url": f"/drive/{d.drive.id}",
    }


def pool_json(p: PoolRow) -> dict[str, Any]:
    s = p.latest
    return {
        "id": p.pool.id,
        "name": p.pool.name,
        "kind": p.pool.kind,
        "health": s.health if s else None,
        "verdict": p.verdict.value,
        "reasons": (s.reasons or "").splitlines() if s else [],
        "scrubbing": p.scrubbing,
        "read_at": iso(s.collected_at) if s else None,
        "url": f"/pool/{p.pool.id}",
    }


def _host_problem(h: HostRow) -> str | None:
    host = h.host
    if host.state in (HostState.UNREACHABLE.value, HostState.BROKEN.value):
        why = f"{host.last_attempt_class}: {host.last_attempt_reason}" if host.last_attempt_class else "see its page"
        return f"host {host.state} since {iso(host.unreachable_since) or 'its last attempt'}: {why}"
    if host.state == HostState.PENDING.value:
        return "host never collected from yet"
    if host.state in (HostState.PAUSED.value, HostState.RETIRED.value):
        return f"host {host.state}: not collected from"
    if h.verdict == Verdict.STALE:
        return f"host not heard from since {_ago(h.age_hours)}"
    return None


def host_json(h: HostRow, drives: list[DriveRow], pools: list[PoolRow], *, detail: bool) -> dict[str, Any]:
    host = h.host
    problems: list[str] = []
    if (problem := _host_problem(h)) is not None:
        problems.append(problem)
    for d in drives:
        if d.verdict not in FINE:
            what = "; ".join(d.reasons) or (f"last read {_ago(d.age_hours)}" if d.verdict == Verdict.STALE else "")
            problems.append(
                f"{d.dev_name or '?'} {d.drive.label} ({d.drive.serial or d.drive.serial_key}): "
                f"{d.verdict.value}" + (f": {what}" if what else "")
            )
    for p in pools:
        if p.verdict not in FINE:
            what = "; ".join((p.latest.reasons or "").splitlines()) if p.latest else ""
            problems.append(f"{p.pool.kind} pool {p.pool.name}: {p.verdict.value}" + (f": {what}" if what else ""))
    verdicts = [h.verdict, *(d.verdict for d in drives), *(p.verdict for p in pools)]
    worst = min(verdicts, key=rank)
    retired_or_paused = host.state in (HostState.PAUSED.value, HostState.RETIRED.value)
    status = host.state if retired_or_paused else ("ok" if worst in FINE else worst.value)
    out: dict[str, Any] = {
        "host": host.name,
        "ok": not problems,
        "status": status,
        "problems": problems,
        "state": host.state,
        "transport": host.transport,
        "last_success_at": iso(host.last_success_at),
        "last_attempt_at": iso(host.last_attempt_at),
        "drive_count": len(drives),
        "pool_count": len(pools),
        "url": f"/host/{host.name}",
        "api": f"/api/v{VERSION}/host/{host.name}",
    }
    if detail:
        out["drives"] = [drive_json(d) for d in drives]
        out["pools"] = [pool_json(p) for p in pools]
    return out

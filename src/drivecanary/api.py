"""The JSON API: what the page says about a host, for another service to ask.

    GET /api/v1/hosts          every host but the retired ones, each without its drives and pools
    GET /api/v1/host/NAME      one host: ok, status, problems, its drives and its pools
    GET /api/v1/openapi.json   this, described for machines (OpenAPI 3); /api/docs for people

`ok` is the one field a monitor needs: true when nothing about the host, its drives or its pools is failing,
warning, unreadable, stale or unknown (a drive asleep in standby is fine). `status` says how bad, in the
page's words; `problems` says what, one line each. The verdicts are the page's own, from the same queries.
The models below are the response schema: their field descriptions are what /api/v1/openapi.json says.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from drivecanary.models import HostState, Verdict
from drivecanary.queries import DriveRow, HostRow, PoolRow, rank
from drivecanary.timeutil import spelled

VERSION = 1
#: verdicts that are nothing to worry about
FINE = (Verdict.OK, Verdict.SKIPPED)

VerdictName = Literal["ok", "warn", "fail", "error", "stale", "unknown", "skipped"]
StatusName = Literal["ok", "warn", "fail", "error", "stale", "unknown", "paused", "retired"]
DESCRIPTION = """\
The verdicts drivecanary's pages show, as JSON: whether each host, its drives and its pools are healthy, and
if not, why. Read-only, no authentication (it is meant for a LAN).

To watch hosts, poll `GET /api/v1/hosts` and read `ok`; when it is false, each host's `problems` says what is
wrong, and `GET /api/v1/host/{name}` gives that host's drives and pools. A known host always answers 200,
whatever its state: the status code says whether the host exists, `ok` whether it is healthy.

Verdicts, worst first: `fail` (replace the drive, or the pool has lost redundancy), `warn` (a predictive
counter is above zero, a recent error, too hot, worn, a scrub found errors), `error` (smartctl could not read
the drive, or the host could not be reached), `stale` (nothing new within the configured hours), `unknown`
(nothing to judge by yet), `skipped` (a drive asleep in standby, left asleep: fine), `ok`."""


class Drive(BaseModel):
    """A drive in service on the host, as its last reading left it."""

    id: int = Field(description="drivecanary's id for the drive; its page is /drive/{id}")
    dev: str | None = Field(description="Device node on the host when last read, e.g. /dev/sda")
    model: str = Field(description="Model, as smartctl names it")
    serial: str = Field(description="Serial number")
    kind: str = Field(description="hdd, ssd or nvme (unknown when only smartd's logs have been read)")
    capacity_bytes: int | None = Field(description="Size in bytes")
    verdict: VerdictName = Field(description="How it is: see the verdicts above")
    reasons: list[str] = Field(description="Why the verdict is not ok, one line each; empty when ok")
    temperature_c: int | None = Field(description="Temperature at the last reading, in °C")
    power_on_hours: int | None = Field(description="Power-on hours at the last reading")
    read_at: datetime | None = Field(description="When the last reading was taken (UTC)")
    self_test_percent: int | None = Field(description="How far along a self-test now running is; null: none")
    url: str = Field(description="Its page on this hub")


class Pool(BaseModel):
    """A ZFS, btrfs or md pool on the host."""

    id: int = Field(description="drivecanary's id for the pool; its page is /pool/{id}")
    name: str = Field(description="Pool name (btrfs: its mount point)")
    kind: Literal["zfs", "btrfs", "md"] = Field(description="What kind of pool")
    health: str | None = Field(description="Its health as the tool reports it, e.g. ONLINE, DEGRADED")
    verdict: VerdictName = Field(description="How it is: see the verdicts above")
    reasons: list[str] = Field(description="Why the verdict is not ok, one line each; empty when ok")
    scrubbing: str | None = Field(description="A scrub running: how far, e.g. '18.42%', or '' if not said; null: none")
    read_at: datetime | None = Field(description="When it was last read (UTC)")
    url: str = Field(description="Its page on this hub")


class HostSummary(BaseModel):
    """A host's health in brief."""

    host: str = Field(description="The host's name on the hub")
    ok: bool = Field(description="True when nothing about the host, its drives or its pools needs attention")
    status: StatusName = Field(description="The worst verdict among the host, its drives and pools; or paused/retired")
    problems: list[str] = Field(description="What needs attention, one line each; empty when ok")
    state: Literal["pending", "ok", "unreachable", "broken", "paused", "retired"] = Field(
        description="Whether the hub can collect from it: pending (never yet), ok, unreachable, broken "
        "(reached, but something on the host is wrong), paused or retired (not collected from)"
    )
    transport: Literal["pull", "push"] = Field(description="pull: the hub reaches it over ssh; push: it reports in")
    last_success_at: datetime | None = Field(description="When it was last collected from (UTC)")
    last_attempt_at: datetime | None = Field(description="When collection was last tried (UTC)")
    drive_count: int = Field(description="Drives in service on it")
    pool_count: int = Field(description="Pools on it")
    url: str = Field(description="Its page on this hub")
    api: str = Field(description="This host's detail in this API")


class Host(HostSummary):
    """A host's health, with its drives and pools."""

    drives: list[Drive] = Field(description="Its drives in service, worst first")
    pools: list[Pool] = Field(description="Its pools, worst first")


class HostList(BaseModel):
    ok: bool = Field(description="True when every host listed is ok")
    hosts: list[HostSummary] = Field(description="Every host but the retired ones, by name")


class Error(BaseModel):
    error: str = Field(description="What went wrong, e.g. 'no such host: NAME'")


def _ago(hours: float | None) -> str:
    if hours is None:
        return "never"
    return f"{spelled(hours) or f'{hours:.1f} hours'} ago"


def drive(d: DriveRow) -> Drive:
    r = d.latest
    return Drive(
        id=d.drive.id,
        dev=d.dev_name,
        model=d.drive.model or d.drive.model_key,
        serial=d.drive.serial or d.drive.serial_key,
        kind=d.drive.kind,
        capacity_bytes=d.drive.capacity_bytes,
        verdict=d.verdict.value,
        reasons=d.reasons,
        temperature_c=r.temp_c if r else None,
        power_on_hours=r.power_on_hours if r else None,
        read_at=r.collected_at if r else None,
        self_test_percent=d.testing,
        url=f"/drive/{d.drive.id}",
    )


def pool(p: PoolRow) -> Pool:
    s = p.latest
    return Pool(
        id=p.pool.id,
        name=p.pool.name,
        kind=p.pool.kind,
        health=s.health if s else None,
        verdict=p.verdict.value,
        reasons=(s.reasons or "").splitlines() if s else [],
        scrubbing=p.scrubbing,
        read_at=s.collected_at if s else None,
        url=f"/pool/{p.pool.id}",
    )


def _host_problem(h: HostRow) -> str | None:
    host = h.host
    if host.state in (HostState.UNREACHABLE.value, HostState.BROKEN.value):
        why = f"{host.last_attempt_class}: {host.last_attempt_reason}" if host.last_attempt_class else "see its page"
        since = host.unreachable_since.strftime("%Y-%m-%dT%H:%M:%SZ") if host.unreachable_since else "its last attempt"
        return f"host {host.state} since {since}: {why}"
    if host.state == HostState.PENDING.value:
        return "host never collected from yet"
    if host.state in (HostState.PAUSED.value, HostState.RETIRED.value):
        return f"host {host.state}: not collected from"
    if h.verdict == Verdict.STALE:
        return f"host not heard from since {_ago(h.age_hours)}"
    return None


def summary(h: HostRow, drives: list[DriveRow], pools: list[PoolRow]) -> HostSummary:
    host = h.host
    problems: list[str] = []
    if (problem := _host_problem(h)) is not None:
        problems.append(problem)
    for d in drives:
        if d.verdict not in FINE:
            what = "; ".join(d.reasons) or (f"last read {_ago(d.age_hours)}" if d.verdict == Verdict.STALE else "")
            name = f"{d.dev_name or '?'} {d.drive.label} ({d.drive.serial or d.drive.serial_key})"
            problems.append(f"{name}: {d.verdict.value}" + (f": {what}" if what else ""))
    for p in pools:
        if p.verdict not in FINE:
            what = "; ".join((p.latest.reasons or "").splitlines()) if p.latest else ""
            problems.append(f"{p.pool.kind} pool {p.pool.name}: {p.verdict.value}" + (f": {what}" if what else ""))
    worst = min([h.verdict, *(d.verdict for d in drives), *(p.verdict for p in pools)], key=rank)
    if host.state in (HostState.PAUSED.value, HostState.RETIRED.value):
        status = host.state
    else:
        status = "ok" if worst in FINE else worst.value
    return HostSummary(
        host=host.name,
        ok=not problems,
        status=status,
        problems=problems,
        state=host.state,
        transport=host.transport,
        last_success_at=host.last_success_at,
        last_attempt_at=host.last_attempt_at,
        drive_count=len(drives),
        pool_count=len(pools),
        url=f"/host/{host.name}",
        api=f"/api/v{VERSION}/host/{host.name}",
    )


def detail(h: HostRow, drives: list[DriveRow], pools: list[PoolRow]) -> Host:
    return Host(
        **summary(h, drives, pools).model_dump(),
        drives=[drive(d) for d in drives],
        pools=[pool(p) for p in pools],
    )

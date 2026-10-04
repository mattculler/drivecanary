"""One page that says how the drives are; a page per drive with its trends; the hosts and what happened
on each attempt; the collection runs. LAN only, no auth, read-only: the collector writes, this reads."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from drivecanary import __version__, queries
from drivecanary.config import Config, load_config
from drivecanary.db import make_engine, sessionmaker_for
from drivecanary.models import AttrlogCursor, CollectionRun, Drive, Host, HostAttempt, Pool, Verdict
from drivecanary.pools import pool_members
from drivecanary.scrub import running as scrub_running
from drivecanary.scrub import summarize as summarize_scrub
from drivecanary.smart import ATTR_LABELS, ERROR_KINDS, UNKNOWN_NAMES, SmartReport, fahrenheit
from drivecanary.timeutil import hours_ago, minutes_taking, shown, spelled, utcnow, with_span

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"


def get_session(request: Request) -> Iterator[Session]:
    s = request.app.state.factory()
    try:
        yield s
    finally:
        s.close()


Db = Annotated[Session, Depends(get_session)]


def fmt_bytes(n: int | None) -> str:
    if not n:
        return ""
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if n >= size:
            v = n / size
            return f"{v:.1f} {unit}" if v < 100 else f"{v:.0f} {unit}"
    return f"{n} B"


def fmt_ago(dt: datetime | None, now: datetime | None = None) -> str:
    h = hours_ago(dt, now)
    if h is None:
        return "never"
    if h < 1 / 60:
        return "just now"
    if h < 1:
        return f"{h * 60:.0f} min ago"
    if h < 48:
        return f"{h:.1f} h ago"
    return f"{spelled(h)} ago"


def fmt_temp(c: int | float | None) -> str:
    """Celsius as the drive said it, and Fahrenheit for the room it is in."""
    return f"{c:g} °C ({fahrenheit(c)} °F)" if c is not None else ""


def fmt_temp_cell(c: int | float | None) -> Markup:
    """The same, in a column headed °C (°F)."""
    return Markup('{:g} <span class="muted">({})</span>').format(c, fahrenheit(c)) if c is not None else Markup("")


def fmt_hours(h: int | None) -> str:
    return with_span(h)


def fmt_hours_cell(h: int | None) -> Markup:
    """A count of hours in a column, the span muted after it."""
    if h is None:
        return Markup("")
    said = spelled(h)
    return Markup('{:,} <span class="muted">({})</span>').format(h, said) if said else Markup("{:,}").format(h)


COUNTERS = (5, 187, 188, 197, 198, 199, 193)
#: wear attributes by id, for a drive nothing but an attribute log has been read from: no names to go by
WEAR_IDS = (177, 231, 202)


def attr_names(report: SmartReport | None) -> dict[int, str]:
    """What each attribute is called on this drive, as smartctl's drive database has it."""
    return {a.id: a.name for a in report.attrs if a.name not in UNKNOWN_NAMES} if report else {}


def metrics_for(drive: Drive, attr_ids: set[int], report: SmartReport | None = None) -> list[dict[str, str]]:
    """Which trend charts a drive page shows: the temperature and hours for every kind, then what says how
    worn it is, then the counters that matter for its kind and that it actually reports."""
    names = attr_names(report)

    def label(attr_id: int) -> str:
        return f"{names.get(attr_id) or ATTR_LABELS.get(attr_id, 'Attribute')} ({attr_id})"

    out = [
        {"metric": "temp", "label": "Temperature", "unit": "°C"},
        {"metric": "poh", "label": "Power-on hours", "unit": "h"},
    ]
    if drive.kind == "nvme":
        for key in ("nvme_percentage_used", "nvme_available_spare", "nvme_media_errors", "nvme_err_log_entries"):
            title, unit = queries.RUN_METRICS[key]
            out.append({"metric": key, "label": title, "unit": unit})
    elif (drive.protocol or "").upper() == "SCSI":
        title, unit = queries.RUN_METRICS["scsi_grown_defects"]
        out.append({"metric": "scsi_grown_defects", "label": title, "unit": unit})
    else:
        if report is not None and report.endurance_used is not None:
            out.append({"metric": "endurance_used", "label": "Endurance used (the standard figure)", "unit": "%"})
        # the vendor's own: the normalized value counts down from 100 whatever is counted in the raw
        wear = [a.id for a in report.wear_attrs] if report else [i for i in WEAR_IDS if i in attr_ids]
        for attr_id in wear:
            if attr_id in attr_ids:
                out.append({"metric": f"attr:{attr_id}:value", "label": f"{label(attr_id)}, normalized", "unit": ""})
        for attr_id in COUNTERS:
            if attr_id in attr_ids and attr_id not in wear:
                out.append({"metric": f"attr:{attr_id}", "label": label(attr_id), "unit": ""})
        if report is not None and report.ata_error_count:
            out.append({"metric": "ata_errors", "label": "Error log entries", "unit": ""})
    return out


HOST_SCRIPTS = Path(__file__).resolve().parents[3] / "deploy" / "host"


def script_versions() -> dict[str, int]:
    """The versions of the host scripts in this checkout: what a host would have after install-host.sh."""
    found: dict[str, int] = {}
    for name in ("gate", "probe", "agent"):
        try:
            m = re.search(r"^VERSION=(\d+)$", (HOST_SCRIPTS / name).read_text(), re.M)
        except OSError:
            continue
        if m:
            found[name] = int(m.group(1))
    return found


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    engine = make_engine(cfg.db_path, echo=cfg.db.echo_sql)
    app = FastAPI(title="drivecanary", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = cfg
    app.state.factory = sessionmaker_for(engine)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["bytes"] = fmt_bytes
    templates.env.filters["ago"] = fmt_ago
    templates.env.filters["dt"] = lambda dt: shown(dt, cfg.web.timezone)
    templates.env.filters["hours"] = fmt_hours
    templates.env.filters["temp"] = fmt_temp
    templates.env.filters["hourscell"] = fmt_hours_cell
    templates.env.filters["spelled"] = spelled
    templates.env.filters["taking"] = minutes_taking
    templates.env.filters["tempcell"] = fmt_temp_cell
    templates.env.filters["scrub"] = lambda text, kind: summarize_scrub(kind, text)
    templates.env.globals["version"] = __version__
    templates.env.globals["attr_label"] = lambda i: ATTR_LABELS.get(i, f"Attribute {i}")

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"cfg": cfg, "now": utcnow(), **ctx})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, db: Db) -> HTMLResponse:
        ov = queries.overview(db, cfg)
        return page(request, "index.html", ov=ov)

    @app.get("/drive/{drive_id}", response_class=HTMLResponse)
    def drive_page(request: Request, db: Db, drive_id: int, window: str = "30d") -> HTMLResponse:
        drive = db.get(Drive, drive_id)
        if drive is None:
            raise HTTPException(404, "no such drive")
        latest = queries.latest_run(db, drive.id)
        attrs = queries.run_attrs(db, latest) if latest else []
        runs = queries.drive_runs(db, drive.id, 30)
        first, last, count = queries.sample_span(db, drive.id)
        age = hours_ago(latest.collected_at) if latest else None
        verdict = Verdict(latest.verdict) if latest else Verdict.UNKNOWN
        if age is not None and age > cfg.collect.stale_after_hours and verdict != Verdict.FAIL:
            verdict = Verdict.STALE
        report = queries.latest_report(db, drive.id)
        logged: dict[str, int] = {}
        for _, kind in report.error_entries if report else []:
            logged[kind] = logged.get(kind, 0) + 1
        return page(
            request,
            "drive.html",
            drive=drive,
            latest=latest,
            verdict=verdict,
            attrs=attrs,
            report=report,
            testing=queries.running_selftest(db, drive.id, cfg, utcnow()),
            selftest=queries.drive_selftest(db, cfg, drive, utcnow()),
            attrlogs=queries.drive_attrlogs(db, drive),
            names=attr_names(report),
            logged=logged,
            error_kinds=ERROR_KINDS,
            runs=runs,
            sightings=queries.drive_sightings(db, drive.id),
            span=(first, last, count),
            metrics=metrics_for(drive, {a.attr_id for a in attrs}, report),
            window=window if window in ("7d", "30d", "90d", "1y", "all") else "30d",
        )

    @app.get("/api/drives/{drive_id}/series")
    def drive_series(db: Db, drive_id: int, metric: Annotated[str, Query()], window: str = "30d") -> JSONResponse:
        if db.get(Drive, drive_id) is None:
            raise HTTPException(404, "no such drive")
        try:
            pts = queries.series(db, drive_id, metric, queries.since_for(window, utcnow()), cfg.web.series_max_points)
        except (KeyError, ValueError):
            raise HTTPException(400, f"unknown metric {metric!r}") from None
        return JSONResponse({"metric": metric, "window": window, "points": pts})

    @app.get("/drives/retired", response_class=HTMLResponse)
    def retired_page(request: Request, db: Db) -> HTMLResponse:
        return page(request, "retired.html", drives=queries.retired_drive_rows(db, cfg, utcnow()))

    def hub_identity(request: Request) -> dict[str, str | None]:
        """What a host's install needs to know of this hub: its public key, and its address when the page was
        asked for by a LAN address."""
        hub_key = cfg.hub_key_copy.read_text().strip() if cfg.hub_key_copy.is_file() else None
        asked = request.url.hostname or ""
        is_lan_ip = re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", asked) and not asked.startswith("127.")
        return {"hub_key": hub_key, "hub_ip": asked if is_lan_ip else None}

    @app.get("/hosts/retired", response_class=HTMLResponse)
    def retired_hosts_page(request: Request, db: Db) -> HTMLResponse:
        ov = queries.overview(db, cfg)
        drives: dict[int, int] = {}
        for d in queries.retired_drive_rows(db, cfg, utcnow()):
            if d.host is not None:
                drives[d.host.id] = drives.get(d.host.id, 0) + 1
        hosts = sorted((h for h in ov.hosts if h.host.state == "retired"), key=lambda h: h.host.name)
        return page(request, "retired_hosts.html", hosts=hosts, drives=drives)

    @app.get("/hosts", response_class=HTMLResponse)
    def hosts_page(request: Request, db: Db) -> HTMLResponse:
        return page(request, "hosts.html", ov=queries.overview(db, cfg), **hub_identity(request))

    @app.get("/host/{ref}", response_class=HTMLResponse)
    def host_page(request: Request, db: Db, ref: str) -> HTMLResponse:
        # by name, so that a link can be written from elsewhere without knowing a number; a number still works
        host = db.scalar(select(Host).where(Host.name == ref))
        if host is None and ref.isdigit():
            host = db.get(Host, int(ref))
        if host is None:
            raise HTTPException(404, "no such host")
        attempts = list(
            db.scalars(
                select(HostAttempt)
                .where(HostAttempt.host_id == host.id)
                .order_by(HostAttempt.started_at.desc())
                .limit(40)
            )
        )
        ov = queries.overview(db, cfg)
        drives = [d for d in ov.drives if d.host is not None and d.host.id == host.id]
        pools = [p for p in ov.pools if p.host.id == host.id]
        cursors = list(db.scalars(select(AttrlogCursor).where(AttrlogCursor.host_id == host.id)))
        installed = {"gate": host.gate_version, "probe": host.probe_version, "agent": host.agent_version}
        current = script_versions()
        behind = sorted(k for k, v in installed.items() if v is not None and current.get(k, 0) > v)
        return page(
            request,
            "host.html",
            host=host,
            attempts=attempts,
            drives=drives,
            pools=pools,
            cursors=cursors,
            installed=installed,
            selftests=queries.host_selftests(db, cfg, host, utcnow()),
            retired=[d for d in queries.retired_drive_rows(db, cfg, utcnow()) if d.host and d.host.id == host.id],
            **hub_identity(request),
            current=current,
            behind=behind,
        )

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, db: Db) -> HTMLResponse:
        runs = list(db.scalars(select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(60)))
        return page(request, "runs.html", runs=runs)

    @app.get("/pool/{pool_id}", response_class=HTMLResponse)
    def pool_page(request: Request, db: Db, pool_id: int) -> HTMLResponse:
        pool = db.get(Pool, pool_id)
        if pool is None:
            raise HTTPException(404, "no such pool")
        host = db.get(Host, pool.host_id)
        latest = queries.latest_pool_status(db, pool.id)
        members, others = pool_members(db, pool, latest)
        rows = {d.drive.id: d for d in queries.overview(db, cfg).drives}
        fresh = latest is not None and (hours_ago(latest.collected_at) or 0) <= cfg.collect.stale_after_hours
        scrubbing = scrub_running(pool.kind, latest.scrub) if latest is not None and fresh else None
        return page(
            request,
            "pool.html",
            pool=pool,
            host=host,
            latest=latest,
            members=members,
            others=others,
            rows=rows,
            scrubbing=scrubbing,
        )

    @app.get("/healthz")
    def healthz(db: Db) -> JSONResponse:
        last = db.scalar(select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(1))
        return JSONResponse(
            {
                "ok": True,
                "version": __version__,
                "last_collection_age_hours": hours_ago(last.finished_at or last.started_at) if last else None,
            }
        )

    @app.get("/favicon.ico")
    def favicon() -> Response:
        """For whatever asks for the usual path without reading the page; the pages name their icons."""
        return FileResponse(STATIC / "favicon-32.png", media_type="image/png")

    return app

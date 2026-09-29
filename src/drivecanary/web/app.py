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
from sqlalchemy import select
from sqlalchemy.orm import Session

from drivecanary import __version__, queries
from drivecanary.config import Config, load_config
from drivecanary.db import make_engine, sessionmaker_for
from drivecanary.models import AttrlogCursor, CollectionRun, Drive, Host, HostAttempt, Pool, Verdict
from drivecanary.scrub import summarize as summarize_scrub
from drivecanary.smart import ATTR_LABELS
from drivecanary.timeutil import hours_ago, utcnow

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
    return f"{h / 24:.0f} d ago"


def fmt_dt(dt: datetime | None) -> str:
    return f"{dt:%Y-%m-%d %H:%M} UTC" if dt else ""


def fmt_hours(h: int | None) -> str:
    if h is None:
        return ""
    return f"{h:,} h ({h / 24 / 365.25:.1f} y)" if h >= 8760 else f"{h:,} h"


def metrics_for(drive: Drive, attr_ids: set[int]) -> list[dict[str, str]]:
    """Which trend charts a drive page shows: the temperature and hours for every kind, then the counters
    that matter for its kind and that it actually reports."""
    out = [
        {"metric": "temp", "label": "Temperature", "unit": "°C"},
        {"metric": "poh", "label": "Power-on hours", "unit": "h"},
    ]
    if drive.kind == "nvme":
        for key in ("nvme_percentage_used", "nvme_available_spare", "nvme_media_errors", "nvme_err_log_entries"):
            label, unit = queries.RUN_METRICS[key]
            out.append({"metric": key, "label": label, "unit": unit})
    elif (drive.protocol or "").upper() == "SCSI":
        label, unit = queries.RUN_METRICS["scsi_grown_defects"]
        out.append({"metric": "scsi_grown_defects", "label": label, "unit": unit})
    else:
        # wear, for solid state: the normalized value counts down from 100 whatever the vendor counts in the raw
        for attr_id in (177, 231, 233, 202):
            if attr_id in attr_ids:
                label = f"{ATTR_LABELS.get(attr_id, 'Attribute')} ({attr_id}), normalized"
                out.append({"metric": f"attr:{attr_id}:value", "label": label, "unit": ""})
        for attr_id in (5, 187, 188, 197, 198, 199, 193):
            if attr_id in attr_ids:
                out.append(
                    {
                        "metric": f"attr:{attr_id}",
                        "label": f"{ATTR_LABELS.get(attr_id, 'Attribute')} ({attr_id})",
                        "unit": "",
                    }
                )
    return out


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
    templates.env.filters["dt"] = fmt_dt
    templates.env.filters["hours"] = fmt_hours
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
        return page(
            request,
            "drive.html",
            drive=drive,
            latest=latest,
            verdict=verdict,
            attrs=attrs,
            runs=runs,
            sightings=queries.drive_sightings(db, drive.id),
            span=(first, last, count),
            metrics=metrics_for(drive, {a.attr_id for a in attrs}),
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

    @app.get("/hosts", response_class=HTMLResponse)
    def hosts_page(request: Request, db: Db) -> HTMLResponse:
        ov = queries.overview(db, cfg)
        hub_key = cfg.hub_key_copy.read_text().strip() if cfg.hub_key_copy.is_file() else None
        # the address this page was asked for is the hub's, when it was asked for by address
        asked = request.url.hostname or ""
        is_lan_ip = re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", asked) and not asked.startswith("127.")
        hub_ip = asked if is_lan_ip else None
        return page(request, "hosts.html", ov=ov, hub_key=hub_key, hub_ip=hub_ip)

    @app.get("/host/{host_id}", response_class=HTMLResponse)
    def host_page(request: Request, db: Db, host_id: int) -> HTMLResponse:
        host = db.get(Host, host_id)
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
        return page(request, "host.html", host=host, attempts=attempts, drives=drives, pools=pools, cursors=cursors)

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
        return page(request, "pool.html", pool=pool, host=host, latest=latest)

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

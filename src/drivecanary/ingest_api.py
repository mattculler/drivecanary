"""The ingest listener: one endpoint that push agents POST their envelope to.

A service of its own (`drivecanary ingest serve`, drivecanary-ingest.service), run by the collector user,
so that the page stays read-only and the thing that accepts writes from the network is as small as it can
be: read a bounded body, find the host by its token, hand over to drivecanary.push.
"""

from __future__ import annotations

import threading

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse

from drivecanary import __version__, push
from drivecanary.config import Config, load_config
from drivecanary.db import make_engine, sessionmaker_for


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def _count(header: str | None) -> int:
    return int(header) if header and header.isdigit() else 0


def create_ingest_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    engine = make_engine(cfg.db_path, echo=cfg.db.echo_sql)
    factory = sessionmaker_for(engine)
    one_at_a_time = threading.Lock()  # a first backfill takes seconds to store; pushes queue, they do not collide
    cap = cfg.ingest.max_body_mb * 1024 * 1024
    app = FastAPI(title="drivecanary ingest", docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/api/ingest")
    async def ingest(request: Request) -> PlainTextResponse:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap:
            return PlainTextResponse(f"refused: body larger than {cfg.ingest.max_body_mb} MB\n", status_code=413)
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > cap:
                return PlainTextResponse(f"refused: body larger than {cfg.ingest.max_body_mb} MB\n", status_code=413)
            chunks.append(chunk)

        def store() -> push.PushReply:
            with one_at_a_time:
                return push.receive(
                    cfg,
                    factory,
                    token=_bearer(request.headers.get("authorization")),
                    body=b"".join(chunks),
                    encoding=request.headers.get("content-encoding"),
                    agent_failures=_count(request.headers.get("x-agent-failures")),
                    agent_last_failure=request.headers.get("x-agent-last-failure"),
                )

        reply = await run_in_threadpool(store)
        return PlainTextResponse(reply.text, status_code=reply.status)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__, "service": "ingest"})

    return app

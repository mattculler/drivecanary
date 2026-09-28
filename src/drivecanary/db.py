"""Engine and session helpers. One SQLite file in WAL mode, foreign keys on."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from drivecanary.config import Config

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def make_engine(path: Path | str, *, echo: bool = False) -> Engine:
    url = "sqlite+pysqlite:///:memory:" if str(path) == ":memory:" else f"sqlite+pysqlite:///{path}"
    engine = create_engine(url, echo=echo, future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        # WAL: the hourly collector writes while pages are read, and neither waits on the other
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    return engine


def init_engine(config: Config) -> Engine:
    global _engine, _factory
    if _engine is None:
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = make_engine(config.db_path, echo=config.db.echo_sql)
        _factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("init_engine(config) has not been called")
    return _engine


def sessionmaker_for(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    if _factory is None:
        raise RuntimeError("init_engine(config) has not been called")
    s = _factory()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


def snapshot(db_path: Path, dest: Path) -> str:
    """A consistent copy of the live database for the backup bundle, via SQLite's backup API (a plain
    copy of a WAL database loses whatever is still in the WAL). The copy is switched to rollback journal
    mode so opening it later, on a NAS share, creates no -wal/-shm files, and its integrity is checked.
    Returns the integrity_check result ('ok')."""
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        row = dst.execute("PRAGMA integrity_check").fetchone()
        result = str(row[0]) if row else "no result"
    finally:
        dst.close()
        src.close()
    if result != "ok":
        raise RuntimeError(f"snapshot integrity_check: {result}")
    return result

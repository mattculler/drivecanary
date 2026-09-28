"""Alembic environment. The database path comes from the drivecanary config, or from the
DRIVECANARY_DB environment variable (the test suite and `make dev-db` use it)."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context

from drivecanary.db import make_engine
from drivecanary.models import Base

config = context.config
if config.config_file_name is not None and not config.attributes.get("skip_logging"):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _db_path() -> str:
    env = os.environ.get("DRIVECANARY_DB")
    if env:
        return env
    from drivecanary.config import load_config

    return str(load_config().db_path)


def run_migrations_offline() -> None:
    context.configure(
        url=f"sqlite:///{_db_path()}",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # SQLite cannot ALTER most things in place: batch mode rebuilds a table behind a migration that needs it
    engine = make_engine(_db_path())
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

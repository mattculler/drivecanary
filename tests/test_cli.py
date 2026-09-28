from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from drivecanary.cli import app
from drivecanary.config import example_config, render_toml
from tests.conftest import FIXTURES

runner = CliRunner()


def _config(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(render_toml(example_config(tmp_path / "state")))
    return p


def test_end_to_end_without_a_network(tmp_path: Path) -> None:
    c = str(_config(tmp_path))
    r = runner.invoke(app, ["-c", c, "config", "check"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["-c", c, "db", "migrate"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["-c", c, "db", "current"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app, ["-c", c, "host", "add", "atlas", "--address", "atlas.domain", "--tz", "America/New_York", "--no-keyscan"]
    )
    assert r.exit_code == 0, r.output
    assert (tmp_path / "state" / "ssh" / "config").read_text().count("Host atlas") == 1
    r = runner.invoke(app, ["-c", c, "host", "add", "atlas", "--no-keyscan"])
    assert r.exit_code == 2
    r = runner.invoke(app, ["-c", c, "host", "add", "x", "--tz", "Mars/Olympus", "--no-keyscan"])
    assert r.exit_code != 0
    f = FIXTURES / "attrlog" / "atlas" / "attrlog.ST20000NM007D_3DJ103-ZXA00001.ata.csv"
    r = runner.invoke(app, ["-c", c, "import", "attrlog", str(f), "--host", "atlas"])
    assert r.exit_code == 0 and "20 lines, 20 added" in r.output, r.output
    r = runner.invoke(app, ["-c", c, "import", "attrlog", str(f), "--host", "atlas"])
    assert "0 added, 20 already stored" in r.output
    r = runner.invoke(app, ["-c", c, "status"])
    # rich wraps the 80-column table mid-word, so look for the serial, the host and the verdict (2023 data is stale)
    assert r.exit_code == 0 and "ZXA00001" in r.output and "atlas" in r.output and "STALE" in r.output, r.output
    r = runner.invoke(app, ["-c", c, "host", "set", "atlas", "--state", "paused", "--note", "off for the summer"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["-c", c, "host", "list"])
    assert "paused" in r.output
    r = runner.invoke(app, ["-c", c, "host", "remove", "atlas"])
    assert r.exit_code == 2 and "retire it instead" in r.output, "its attrlog readings reference it"
    r = runner.invoke(app, ["-c", c, "host", "set", "atlas", "--state", "retired"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["-c", c, "host", "add", "empty", "--no-keyscan"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["-c", c, "host", "remove", "empty"])
    assert r.exit_code == 0 and "removed empty" in r.output, r.output
    r = runner.invoke(app, ["-c", c, "db", "snapshot", str(tmp_path / "snap.db")])
    assert r.exit_code == 0 and (tmp_path / "snap.db").exists() and not (tmp_path / "snap.db-wal").exists()


def test_config_example_matches_the_file() -> None:
    r = runner.invoke(app, ["config", "example"])
    assert r.exit_code == 0 and r.output == render_toml(example_config())


def test_migration_matches_the_models(tmp_path: Path) -> None:
    """The initial migration and the models agree; a model change without a migration fails here."""
    import os

    from alembic.autogenerate import compare_metadata
    from alembic.command import upgrade
    from alembic.config import Config as AlembicConfig
    from alembic.runtime.migration import MigrationContext

    from drivecanary.cli import ALEMBIC_INI
    from drivecanary.db import make_engine
    from drivecanary.models import Base

    db = tmp_path / "m.db"
    os.environ["DRIVECANARY_DB"] = str(db)
    upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(db)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    engine.dispose()
    assert diff == [], diff

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


def test_push_hosts_get_a_token_and_are_not_pulled(tmp_path: Path) -> None:
    import re

    from sqlalchemy import select

    from drivecanary.config import load_config
    from drivecanary.db import make_engine, sessionmaker_for
    from drivecanary.models import Host
    from drivecanary.push import hash_token

    c = str(_config(tmp_path))
    assert runner.invoke(app, ["-c", c, "db", "migrate"]).exit_code == 0
    r = runner.invoke(app, ["-c", c, "host", "add", "pve", "--transport", "push"])
    assert r.exit_code == 0 and "install-host.sh pve --push --hub-url" in r.output, r.output
    token = re.search(r"--token (\S+)", r.output)
    assert token is not None
    engine = make_engine(load_config(Path(c)).db_path)

    def stored(name: str) -> Host:
        with sessionmaker_for(engine)() as s:
            host = s.scalar(select(Host).where(Host.name == name))
            assert host is not None
            return host

    assert stored("pve").push_token_hash == hash_token(token.group(1)) and stored("pve").hostkey is None
    assert "pve" not in (tmp_path / "state" / "ssh" / "config").read_text(), "no ssh to a push host"
    r = runner.invoke(app, ["-c", c, "collect", "--host", "pve"])
    assert r.exit_code == 2 and "reports by itself" in r.output
    r = runner.invoke(app, ["-c", c, "host", "token", "pve"])
    rotated = re.search(r"--token (\S+)", r.output)
    assert rotated is not None and rotated.group(1) != token.group(1)
    assert stored("pve").push_token_hash == hash_token(rotated.group(1))
    assert runner.invoke(app, ["-c", c, "host", "add", "atlas", "--no-keyscan"]).exit_code == 0
    assert runner.invoke(app, ["-c", c, "host", "token", "atlas"]).exit_code == 2
    r = runner.invoke(app, ["-c", c, "host", "set", "atlas", "--transport", "push"])
    assert r.exit_code == 0 and "--token" in r.output and stored("atlas").push_token_hash
    r = runner.invoke(app, ["-c", c, "host", "set", "atlas", "--transport", "pull"])
    assert r.exit_code == 0 and stored("atlas").push_token_hash is None
    engine.dispose()


def test_a_pasted_ssh_keygen_line_is_a_fingerprint() -> None:
    """`ssh-keygen -lf` prints the size before the fingerprint and the key comment after it; pasting any of
    that must compare equal to the bare token (2026-09-28: '... does not match ... root@atlas')."""
    import pytest
    import typer

    from drivecanary.cli import _wanted_fingerprint

    fp = "SHA256:5F9gtX2U0fwtSIFZ37XXZY/rJ1kvgeMQ23r2Xf6u8lg"
    for pasted in (fp, f"{fp} root@atlas", f"256 {fp} root@atlas (ED25519)", f"  {fp}\n", f"{fp}="):
        assert _wanted_fingerprint(pasted) == fp, pasted
    assert _wanted_fingerprint(None) is None
    with pytest.raises(typer.Exit):
        _wanted_fingerprint("MD5:aa:bb:cc")


def test_imports_default_to_the_configured_zone(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import select

    from drivecanary.config import load_config
    from drivecanary.db import make_engine, sessionmaker_for
    from drivecanary.models import SmartRun

    c = _config(tmp_path)
    assert load_config(c).collect.default_tz == "America/New_York"
    assert runner.invoke(app, ["-c", str(c), "db", "migrate"]).exit_code == 0
    f = FIXTURES / "attrlog" / "atlas" / "attrlog.ST20000NM007D_3DJ103-ZXA00001.ata.csv"
    r = runner.invoke(app, ["-c", str(c), "import", "attrlog", str(f)])  # no --host, no --tz
    assert r.exit_code == 0 and "20 added" in r.output, r.output
    engine = make_engine(load_config(c).db_path)
    with sessionmaker_for(engine)() as s:
        first = s.scalar(select(SmartRun.collected_at).order_by(SmartRun.collected_at).limit(1))
    engine.dispose()
    assert first == datetime(2023, 12, 18, 2, 45, 35, tzinfo=UTC)  # 21:45:35 EST


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

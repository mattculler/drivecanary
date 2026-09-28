from __future__ import annotations

from pathlib import Path

import pytest

from drivecanary.config import Config, ConfigError, example_config, iter_keys, load_config, render_toml

ROOT = Path(__file__).resolve().parents[1]


def test_committed_example_is_up_to_date() -> None:
    """deploy/config.example.toml is rendered from the model; `make config-example` regenerates it."""
    assert (ROOT / "deploy" / "config.example.toml").read_text() == render_toml(example_config())


def test_example_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(render_toml(example_config()))
    cfg = load_config(p)
    assert cfg.db_path == Path("/var/lib/drivecanary/drivecanary.db")
    assert cfg.ssh_dir == Path("/var/lib/drivecanary/ssh")
    assert cfg.collect.parallel == 4
    assert cfg.status.ata_warn_attributes == [5, 187, 188, 197, 198]


def test_unknown_key_is_an_error(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[web]\nport = 8080\ncolour = "blue"\n')
    with pytest.raises(ConfigError, match="colour"):
        load_config(p)


def test_default_zone_must_exist(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[collect]\ndefault_tz = "Mars/Olympus"\n')
    with pytest.raises(ConfigError, match="unknown time zone"):
        load_config(p)


def test_missing_file_names_the_env_var(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="DRIVECANARY_CONFIG"):
        load_config(tmp_path / "nope.toml")


def test_every_key_is_documented() -> None:
    for k in iter_keys(Config()):
        assert k.description, k.key


def test_dev_example_points_at_the_state_dir(tmp_path: Path) -> None:
    cfg = example_config(tmp_path)
    assert cfg.db_path == tmp_path / "drivecanary.db"
    assert cfg.web.bind == "127.0.0.1"

"""Configuration: one TOML file is the single home for every tunable.

The pydantic model below *is* the documentation. `deploy/config.example.toml` is rendered from it by
`drivecanary config example` and a test asserts the committed file is up to date. Unknown keys anywhere in
the file are a hard error so typos fail loudly. Nothing here is secret: the pull collector holds an ssh key
in [paths].ssh_dir and that is all.
"""

from __future__ import annotations

import json
import os
import textwrap
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticUndefined

DEFAULT_CONFIG_PATH = Path("/etc/drivecanary/config.toml")
CONFIG_ENV = "DRIVECANARY_CONFIG"


class ConfigError(Exception):
    """Raised for a missing file, TOML syntax errors, unknown keys or bad values."""


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


# --------------------------------------------------------------------------- db / paths


class DbConfig(Section):
    """The SQLite database: every host, drive, sample and pool status lives here."""

    path: Path = Field(
        default=Path("drivecanary.db"),
        description=(
            "The database file. A relative path resolves against [paths].state_dir. It is opened in WAL mode: "
            "keep it on local disk, never on NFS (WAL's shared memory does not work across NFS). Backups snapshot "
            "it with SQLite's backup API (deploy/backup.sh); a plain copy of a live WAL database loses rows."
        ),
    )
    echo_sql: bool = Field(default=False, description="Log every SQL statement (debugging only).")


class PathsConfig(Section):
    """Where mutable state lives. Relative paths resolve against state_dir."""

    state_dir: Path = Field(
        default=Path("/var/lib/drivecanary"),
        description="Root for the database, the collector's ssh material and the backups.",
    )
    ssh_dir: Path = Field(
        default=Path("ssh"),
        description=(
            "The collector's ssh material: its private key (id_ed25519), the client config it generates from the "
            "host table, and the known_hosts it pins. Owned by the collector user, mode 0700; the web user "
            "cannot read it."
        ),
    )
    backups_dir: Path = Field(
        default=Path("backups"),
        description="Where deploy/backup.sh keeps the nightly bundles (how many is set in the script).",
    )


# --------------------------------------------------------------------------- collection


class CollectConfig(Section):
    """The pull: one ssh per host, the probe on the other end (docs/transport-design-2026-09-28.md)."""

    parallel: int = Field(
        default=4,
        ge=1,
        le=32,
        description="Hosts contacted at once. The ssh calls run in parallel; the database writes do not (count).",
    )
    ssh_user: str = Field(
        default="drivecanary",
        description=(
            "The account on every monitored host whose forced command is the gate; deploy/host/install-host.sh "
            "creates it. A host can override this in its row."
        ),
    )
    connect_timeout_seconds: int = Field(
        default=10,
        ge=1,
        description=(
            "OpenSSH ConnectTimeout: the TCP connect and banner exchange only, not the remote command (seconds)."
        ),
    )
    host_timeout_seconds: int = Field(
        default=180,
        ge=10,
        description=(
            "The whole ssh call, remote probe included; a hung USB bridge or a drive that will not answer ends "
            "here and the attempt is recorded as a timeout (seconds)."
        ),
    )
    max_output_mb: int = Field(
        default=64,
        ge=1,
        description=(
            "An envelope larger than this is dropped and the attempt fails. A first backfill of years of attrlog "
            "is a few MB per drive, so this is only a guard against a runaway (megabytes)."
        ),
    )
    unreachable_backoff_hours: float = Field(
        default=24.0,
        ge=0,
        description=(
            "A host unreachable for longer than this is tried every retry_every_hours instead of every run (hours)."
        ),
    )
    retry_every_hours: float = Field(
        default=6.0, ge=0.1, description="How often a long-unreachable host is tried again (hours)."
    )
    stale_after_hours: float = Field(
        default=3.0,
        ge=0.1,
        description="A host or drive with no successful sample for this long is shown STALE (hours).",
    )
    default_tz: str = Field(
        default="America/New_York",
        description=(
            "The zone a host's local timestamps (smartd's attrlog lines) are read in when nothing says otherwise. "
            "A zone set on the host's row wins, then the zone the host itself reports, then this (Olson name)."
        ),
    )

    @field_validator("default_tz")
    @classmethod
    def _known_zone(cls, v: str) -> str:
        from drivecanary.timeutil import zone

        zone(v)
        return v


# --------------------------------------------------------------------------- verdicts


class StatusConfig(Section):
    """The OK / WARN / FAIL verdict on a sample (drivecanary/status.py)."""

    ata_warn_attributes: list[int] = Field(
        default_factory=lambda: [5, 187, 188, 197, 198],
        description=(
            "ATA attribute ids whose non-zero raw value is a WARN: Backblaze's five (reallocated sectors, reported "
            "uncorrectable, command timeout, current pending, offline uncorrectable). SMART PASSED alone says "
            "little; these do."
        ),
    )
    temp_warn_c: int = Field(default=55, description="A drive at or above this temperature is a WARN (degrees C).")
    nvme_percentage_used_warn: int = Field(
        default=90,
        ge=1,
        le=255,
        description="NVMe percentage_used at or above this is a WARN; 100 is the vendor's rated endurance (percent).",
    )
    error_log_is_warn: bool = Field(
        default=False,
        description=(
            "Whether an ATA error log with any entry (smartctl exit bit 6) is a WARN. Off by default: old drives "
            "carry historic entries forever, and the count is on the drive page anyway."
        ),
    )


# --------------------------------------------------------------------------- push


class IngestConfig(Section):
    """Where push agents deliver: hosts that are only up on demand, or that should hold no inbound key
    (the hypervisor the hub runs on). A service of its own, run by the collector user; the page stays
    read-only."""

    bind: str = Field(default="0.0.0.0", description="Address the ingest listener binds to.")
    port: int = Field(default=8081, ge=1, le=65535, description="TCP port; the agents' HUB_URL names it.")
    max_body_mb: int = Field(
        default=32,
        ge=1,
        description=(
            "A request body larger than this is refused before it is read. Agents gzip what they send; what it "
            "inflates to is held to [collect].max_output_mb (megabytes)."
        ),
    )


# --------------------------------------------------------------------------- web / logging


class WebConfig(Section):
    """The LAN-only page. No auth."""

    bind: str = Field(default="0.0.0.0", description="Address uvicorn binds to.")
    port: int = Field(default=8080, ge=1, le=65535, description="TCP port.")
    series_max_points: int = Field(
        default=4000,
        ge=100,
        description="A trend series longer than this is bucket-averaged down to it before the chart gets it (points).",
    )


class LoggingConfig(Section):
    level: str = Field(default="info", description="Log level: debug, info, warning, error.")
    format: Literal["auto", "console", "json"] = Field(
        default="auto", description="`auto` = console on a TTY, JSON otherwise (journald gets JSON)."
    )


# --------------------------------------------------------------------------- root


class Config(Section):
    """drivecanary configuration. Every key is documented in place; the units are in the comments."""

    db: DbConfig = Field(default_factory=DbConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    collect: CollectConfig = Field(default_factory=CollectConfig)
    status: StatusConfig = Field(default_factory=StatusConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    def resolve_path(self, p: Path) -> Path:
        return p if p.is_absolute() else self.paths.state_dir / p

    @property
    def db_path(self) -> Path:
        return self.resolve_path(self.db.path)

    @property
    def ssh_dir(self) -> Path:
        return self.resolve_path(self.paths.ssh_dir)

    @property
    def backups_dir(self) -> Path:
        return self.resolve_path(self.paths.backups_dir)

    @property
    def hub_key_copy(self) -> Path:
        """A copy of the collector's PUBLIC key where the page may read it: the hosts page prints it in the
        instructions for adding a host. The ssh dir itself stays closed to the page."""
        return self.paths.state_dir / "hub-key.pub"


# --------------------------------------------------------------------------- loading


def find_config_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    env = os.environ.get(CONFIG_ENV)
    return Path(env) if env else DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> Config:
    p = find_config_path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p} (set ${CONFIG_ENV} or pass --config)")
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: TOML syntax error: {e}") from e
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"{p}:\n" + "\n".join(format_validation_errors(e))) from e


def format_validation_errors(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "(root)"
        out.append(f"  {loc}: {err['msg']}")
    return out


# --------------------------------------------------------------------------- rendering


class KeyDoc(BaseModel):
    key: str
    value: Any
    default: Any
    description: str
    section_doc: str


def _default_of(field: Any) -> Any:
    if field.default_factory is not None:
        return field.default_factory()
    return None if field.default is PydanticUndefined else field.default


def iter_keys(model: BaseModel, prefix: str = "", section_doc: str = "") -> list[KeyDoc]:
    """Flatten a settings model to dotted keys. Nested models recurse."""
    out: list[KeyDoc] = []
    doc = (type(model).__doc__ or section_doc).strip()
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        key = f"{prefix}{name}"
        if isinstance(value, BaseModel):
            out.extend(iter_keys(value, key + ".", doc))
        else:
            default = _default_of(field)
            if isinstance(default, BaseModel):
                default = default.model_dump()
            out.append(
                KeyDoc(key=key, value=value, default=default, description=field.description or "", section_doc=doc)
            )
    return out


def _toml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, Path):
        return json.dumps(str(v))
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list | tuple):
        return "[" + ", ".join(_toml_scalar(x) for x in v) + "]"
    raise TypeError(f"cannot render {type(v).__name__} as TOML")


def _comment(text: str, width: int = 78) -> list[str]:
    return [f"# {line}" if line else "#" for line in textwrap.wrap(text, width=width - 2)]


def render_toml(model: BaseModel, *, path: str = "", header: bool = True) -> str:
    """Render a settings model as a fully commented TOML document.

    Scalars of a table are written before its sub-tables, as TOML requires. Optional keys that are unset
    are written commented out.
    """
    lines: list[str] = []
    if header and not path:
        lines += _comment(
            "drivecanary configuration. This file is the only home for tunables; every key is explained "
            "directly above it with its unit. Unknown keys make the app refuse to start. Hosts are not here: "
            "`drivecanary host add` keeps them in the database."
        )
        lines.append("")
    scalars: list[str] = []
    subtables: list[str] = []
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        full = f"{path}.{name}" if path else name
        desc = field.description or ""
        if isinstance(value, BaseModel):
            subtables.append(render_toml(value, path=full, header=False))
        else:
            if desc:
                scalars += _comment(desc)
            if value is None:
                scalars.append(f"# {name} = ")
            else:
                scalars.append(f"{name} = {_toml_scalar(value)}")
            scalars.append("")
    if path:
        doc = (type(model).__doc__ or "").strip()
        lines.append(f"[{path}]")
        if doc:
            lines += _comment(doc)
        lines.append("")
    lines += scalars
    if subtables:
        lines.append("\n".join(subtables))
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.rstrip() + "\n"


def example_config(state_dir: Path | None = None) -> Config:
    """A Config populated with defaults, for the example file (or a dev config under a given state dir)."""
    cfg = Config()
    if state_dir is not None:
        cfg.paths.state_dir = state_dir
        cfg.web.bind = "127.0.0.1"
    return cfg

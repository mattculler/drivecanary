"""`drivecanary` command line. Standard flags keep their meanings: -h help, -v verbose, -q quiet, -c config."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from drivecanary import __version__
from drivecanary.config import CONFIG_ENV, Config, ConfigError, example_config, iter_keys, load_config, render_toml
from drivecanary.logging import configure_logging, get_logger
from drivecanary.models import Host
from drivecanary.timeutil import shown

ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = ROOT / "alembic.ini"

HELP = {"help_option_names": ["-h", "--help"]}
app = typer.Typer(
    no_args_is_help=True,
    context_settings=HELP,
    add_completion=False,
    help="Drive health for a home LAN: SMART, pool state and trends from every host, on one page.",
)
config_app = typer.Typer(no_args_is_help=True, context_settings=HELP, help="Inspect and validate config.toml.")
db_app = typer.Typer(no_args_is_help=True, context_settings=HELP, help="Database schema and snapshots.")
host_app = typer.Typer(no_args_is_help=True, context_settings=HELP, help="The monitored hosts.")
import_app = typer.Typer(no_args_is_help=True, context_settings=HELP, help="Bring history in by hand.")
web_app = typer.Typer(no_args_is_help=True, context_settings=HELP, help="The web page.")
ingest_app = typer.Typer(no_args_is_help=True, context_settings=HELP, help="Where push agents deliver.")
for name, sub in (
    ("config", config_app),
    ("db", db_app),
    ("host", host_app),
    ("import", import_app),
    ("web", web_app),
    ("ingest", ingest_app),
):
    app.add_typer(sub, name=name)

out = Console()
err = Console(stderr=True)
log = get_logger("cli")


@dataclass
class State:
    config_path: Path | None
    verbosity: int
    _config: Config | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            try:
                self._config = load_config(self.config_path)
            except ConfigError as e:
                err.print(f"[red]config error:[/red] {e}")
                raise typer.Exit(2) from None
            level = {-1: "warning", 0: self._config.logging.level, 1: "debug"}.get(
                max(-1, min(1, self.verbosity)), "info"
            )
            configure_logging(level, self._config.logging.format)
        return self._config


def _state(ctx: typer.Context) -> State:
    return ctx.obj  # type: ignore[no-any-return]


def _factory(state: State) -> sessionmaker[Session]:
    from drivecanary.db import init_engine, sessionmaker_for

    return sessionmaker_for(init_engine(state.config))


def _print_version(value: bool) -> None:
    if value:
        out.print(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config", "-c", help="Config file (default $DRIVECANARY_CONFIG or /etc/drivecanary/config.toml)."
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Warnings only.")] = False,
    version: Annotated[
        bool, typer.Option("--version", help="Print version and exit.", callback=_print_version, is_eager=True)
    ] = False,
) -> None:
    ctx.obj = State(config_path=config, verbosity=(1 if verbose else 0) - (1 if quiet else 0))


@app.command("update")
def update(
    script: Annotated[Path, typer.Option("--script", help="The installed instance's update script.")] = Path(
        "/opt/drivecanary/deploy/update.sh"
    ),
    use_sudo: Annotated[bool, typer.Option("--sudo/--no-sudo", help="Run the script through sudo.")] = True,
) -> None:
    """Update the installed instance: pull, stop the jobs, sync the venv, migrate, restart the web UI, start the
    timers (deploy/update.sh)."""
    if not script.exists():
        err.print(f"[red]{script} not found: this is not an installed instance (see deploy/README.md)[/red]")
        raise typer.Exit(1)
    cmd = [str(script)]
    if use_sudo and os.geteuid() != 0:
        cmd = ["sudo", *cmd]
    raise typer.Exit(subprocess.call(cmd))


# ============================================================================ config


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Effective values with each key's description."""
    cfg = _state(ctx).config
    t = Table(title="effective configuration", show_lines=False)
    t.add_column("key", style="cyan", no_wrap=True)
    t.add_column("value")
    t.add_column("description")
    for k in iter_keys(cfg):
        t.add_row(k.key, str(k.value), k.description)
    out.print(t)


@config_app.command("get")
def config_get(ctx: typer.Context, key: Annotated[str, typer.Argument(help="Dotted key, e.g. db.path.")]) -> None:
    """Print one value, bare (scripts read it)."""
    cfg = _state(ctx).config
    for k in iter_keys(cfg):
        if k.key == key:
            typer.echo(str(k.value))
            return
    err.print(f"[red]no such key: {key}[/red]")
    raise typer.Exit(2)


@config_app.command("check")
def config_check(ctx: typer.Context) -> None:
    """Load and validate the config; exit 2 if it is wrong. Resolved paths are printed."""
    cfg = _state(ctx).config
    typer.echo(f"config ok: db {cfg.db_path}; ssh {cfg.ssh_dir}; backups {cfg.backups_dir}")


@config_app.command("example")
def config_example(
    state_dir: Annotated[
        Path | None, typer.Option("--state-dir", help="Point the example at this state dir (a dev checkout).")
    ] = None,
) -> None:
    """The documented example config (deploy/config.example.toml is this output)."""
    sys.stdout.write(render_toml(example_config(state_dir)))


# ============================================================================ db


def _alembic_config(cfg: Config) -> object:
    from alembic.config import Config as AlembicConfig

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DRIVECANARY_DB"] = str(cfg.db_path)
    # stdout is passed explicitly: alembic's default binds sys.stdout at import time, which is the wrong
    # stream under any runner that swaps it (the test suite's does)
    ac = AlembicConfig(str(ALEMBIC_INI), stdout=sys.stdout)
    ac.attributes["skip_logging"] = True  # the CLI has configured logging; alembic's fileConfig would fight it
    return ac


@db_app.command("migrate")
def db_migrate(ctx: typer.Context) -> None:
    """Create or upgrade the schema (alembic upgrade head)."""
    from alembic import command

    cfg = _state(ctx).config
    command.upgrade(_alembic_config(cfg), "head")  # type: ignore[arg-type]
    typer.echo(f"migrated {cfg.db_path}")


@db_app.command("current")
def db_current(ctx: typer.Context) -> None:
    """The schema revision the database is at."""
    from alembic import command

    command.current(_alembic_config(_state(ctx).config))  # type: ignore[arg-type]


@db_app.command("snapshot")
def db_snapshot(ctx: typer.Context, dest: Annotated[Path, typer.Argument(help="Where to write the copy.")]) -> None:
    """A consistent copy of the live database (SQLite's backup API), integrity-checked, in rollback-journal
    mode so it can sit on a NAS share without growing -wal/-shm files. deploy/backup.sh calls this."""
    from drivecanary.db import snapshot

    cfg = _state(ctx).config
    result = snapshot(cfg.db_path, dest)
    typer.echo(f"snapshot {dest} ({dest.stat().st_size} bytes, integrity {result})")


# ============================================================================ hosts


def _keyscan(address: str, port: int) -> str:
    cp = subprocess.run(
        ["ssh-keyscan", "-t", "ed25519", "-p", str(port), address],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    lines = [line for line in cp.stdout.splitlines() if line.strip() and not line.startswith("#")]
    if not lines:
        raise RuntimeError(cp.stderr.strip() or f"ssh-keyscan returned no ed25519 key for {address}")
    return lines[0].strip()


def _fingerprint(known_hosts_line: str) -> str:
    cp = subprocess.run(
        ["ssh-keygen", "-lf", "-"], input=known_hosts_line + "\n", capture_output=True, text=True, check=False
    )
    for tok in cp.stdout.split():
        if tok.startswith("SHA256:"):
            return tok
    raise RuntimeError(cp.stderr.strip() or "could not fingerprint the key")


_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}=?")


def _wanted_fingerprint(given: str | None) -> str | None:
    """The SHA256:... token out of whatever was pasted: `ssh-keygen -lf` prints the key size before it and
    the key's comment after it, and a whole pasted line must not read as a mismatch."""
    if given is None:
        return None
    m = _FINGERPRINT.search(given)
    if m is None:
        err.print(f"[red]--fingerprint: no SHA256:... fingerprint in {given!r}[/red]")
        raise typer.Exit(2)
    return m.group(0).rstrip("=")


def _write_ssh_material(state: State, s: Session) -> None:
    from drivecanary.collect import write_ssh_material

    write_ssh_material(state.config, list(s.scalars(select(Host))))


def _host(s: Session, name: str) -> Host:
    host = s.scalar(select(Host).where(Host.name == name))
    if host is None:
        err.print(f"[red]no such host: {name}[/red]")
        raise typer.Exit(2)
    return host


@host_app.command("add")
def host_add(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Short name, also the ssh alias in the generated config.")],
    address: Annotated[str | None, typer.Option("--address", help="Hostname or IP (default: the name).")] = None,
    user: Annotated[
        str | None, typer.Option("--user", help="ssh user on the host (default [collect].ssh_user).")
    ] = None,
    port: Annotated[int, typer.Option("--port")] = 22,
    tz: Annotated[
        str | None,
        typer.Option("--tz", help="The host's zone, if it is not what the host reports or [collect].default_tz."),
    ] = None,
    transport: Annotated[
        str, typer.Option("--transport", help="pull: the hub reaches it over ssh. push: its agent reports in.")
    ] = "pull",
    keyscan: Annotated[
        bool, typer.Option("--keyscan/--no-keyscan", help="Fetch and pin the host's ed25519 key now.")
    ] = True,
    fingerprint: Annotated[
        str | None,
        typer.Option("--fingerprint", help="SHA256:... the scanned key must match (install-host.sh printed it)."),
    ] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Add a monitored host (state `pending` until its first successful collection)."""
    from drivecanary.models import Transport
    from drivecanary.push import new_token
    from drivecanary.timeutil import zone

    state = _state(ctx)
    wanted = _wanted_fingerprint(fingerprint)
    if tz:
        zone(tz)
    if transport not in {t.value for t in Transport}:
        err.print(f"[red]transport must be one of {', '.join(t.value for t in Transport)}[/red]")
        raise typer.Exit(2)
    with _factory(state)() as s:
        if s.scalar(select(Host).where(Host.name == name)) is not None:
            err.print(f"[red]host {name} exists (drivecanary host set {name} ...)[/red]")
            raise typer.Exit(2)
        host = Host(
            name=name, address=address or name, ssh_user=user, ssh_port=port, tz=tz, transport=transport, note=note
        )
        if keyscan and transport == "pull":
            try:
                line = _keyscan(host.address, port)
                fp = _fingerprint(line)
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                err.print(
                    f"[red]keyscan failed: {e}[/red] "
                    f"(add with --no-keyscan and run `drivecanary host keyscan {name}` later)"
                )
                raise typer.Exit(1) from None
            if wanted and fp != wanted:
                err.print(f"[red]host key fingerprint {fp} does not match {wanted}: NOT pinned[/red]")
                raise typer.Exit(1)
            host.hostkey = line
            typer.echo(f"pinned {host.address} {fp}")
        token = None
        if transport == "push":
            token, host.push_token_hash = new_token()
        s.add(host)
        s.commit()
        _write_ssh_material(state, s)
    if token is not None:
        _say_token(state.config, name, token)
        return
    typer.echo(
        f"added {name} ({host.address}, {transport}); it is pending until `drivecanary collect --host {name}` succeeds"
    )


def _say_token(cfg: Config, name: str, token: str) -> None:
    typer.echo(f"{name} is a push host; it is pending until its agent first reports.")
    typer.echo("Its token, shown this once (the hub keeps only a hash):")
    typer.echo(f"  {token}")
    typer.echo("From your workstation, over your own ssh to the host:")
    typer.echo(
        f"  deploy/host/install-host.sh {name} --push --hub-url http://THIS-VM:{cfg.ingest.port} --token {token}"
    )


@host_app.command("token")
def host_token(ctx: typer.Context, name: str) -> None:
    """A new token for a push host (the old one stops working); making a pull host a push host is
    `host set NAME --transport push`."""
    from drivecanary.models import Transport
    from drivecanary.push import new_token

    state = _state(ctx)
    with _factory(state)() as s:
        host = _host(s, name)
        if host.transport != Transport.PUSH.value:
            err.print(f"[red]{name} is a {host.transport} host; tokens are for push hosts[/red]")
            raise typer.Exit(2)
        token, host.push_token_hash = new_token()
        s.commit()
    _say_token(state.config, name, token)


@host_app.command("keyscan")
def host_keyscan(
    ctx: typer.Context,
    name: str,
    fingerprint: Annotated[str | None, typer.Option("--fingerprint", help="SHA256:... the key must match.")] = None,
) -> None:
    """(Re)pin a host's ed25519 key; use after a host is reinstalled."""
    state = _state(ctx)
    wanted = _wanted_fingerprint(fingerprint)
    with _factory(state)() as s:
        host = _host(s, name)
        line = _keyscan(host.address, host.ssh_port)
        fp = _fingerprint(line)
        if wanted and fp != wanted:
            err.print(f"[red]fingerprint {fp} does not match {wanted}: NOT pinned[/red]")
            raise typer.Exit(1)
        host.hostkey = line
        s.commit()
        _write_ssh_material(state, s)
    typer.echo(f"pinned {host.address} {fp}")


@host_app.command("set")
def host_set(
    ctx: typer.Context,
    name: str,
    address: Annotated[str | None, typer.Option("--address")] = None,
    user: Annotated[str | None, typer.Option("--user")] = None,
    port: Annotated[int | None, typer.Option("--port")] = None,
    tz: Annotated[str | None, typer.Option("--tz")] = None,
    state_: Annotated[
        str | None, typer.Option("--state", help="pending, paused (not tried, not counted) or retired (gone for good).")
    ] = None,
    transport: Annotated[
        str | None, typer.Option("--transport", help="pull or push; going to push makes a token.")
    ] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Change a host's row. Setting --state pending re-arms a paused or retired host."""
    from drivecanary.models import HostState, Transport
    from drivecanary.push import new_token
    from drivecanary.timeutil import zone

    state = _state(ctx)
    token = None
    with _factory(state)() as s:
        host = _host(s, name)
        if transport is not None:
            if transport not in {t.value for t in Transport}:
                err.print("[red]--transport takes pull or push[/red]")
                raise typer.Exit(2)
            if transport == Transport.PUSH.value and host.transport != transport:
                token, host.push_token_hash = new_token()
            if transport == Transport.PULL.value:
                host.push_token_hash = None
            host.transport = transport
        was = (host.address, host.ssh_port)
        if address is not None:
            host.address = address
        if user is not None:
            host.ssh_user = user or None
        if port is not None:
            host.ssh_port = port
        if host.hostkey and (host.address, host.ssh_port) != was:
            # the pinned key is filed under the address and port: the same key, under the new ones
            _, _, key = host.hostkey.strip().partition(" ")
            filed = host.address if host.ssh_port == 22 else f"[{host.address}]:{host.ssh_port}"
            host.hostkey = f"{filed} {key}"
        if tz is not None:
            zone(tz)
            host.tz = tz
        if state_ is not None:
            if state_ not in (HostState.PENDING.value, HostState.PAUSED.value, HostState.RETIRED.value):
                err.print("[red]--state takes pending, paused or retired[/red]")
                raise typer.Exit(2)
            host.state = state_
            if state_ == HostState.PENDING.value:
                host.unreachable_since = None
        if note is not None:
            host.note = note or None
        s.commit()
        _write_ssh_material(state, s)
    typer.echo(f"updated {name}")
    if token is not None:
        _say_token(state.config, name, token)


@host_app.command("list")
def host_list(ctx: typer.Context) -> None:
    """Every host, its state and its last attempt."""
    zone_name = _state(ctx).config.web.timezone
    with _factory(_state(ctx))() as s:
        t = Table(title="hosts")
        for col in ("name", "address", "state", "transport", "tz", "last success", "last failure"):
            t.add_column(col)
        for h in s.scalars(select(Host).order_by(Host.name)):
            fail = f"{h.last_attempt_class}: {h.last_attempt_reason}" if h.last_attempt_class else ""
            t.add_row(
                h.name,
                f"{h.address}:{h.ssh_port}" if h.ssh_port != 22 else h.address,
                h.state,
                h.transport,
                h.tz or "",
                shown(h.last_success_at, zone_name) or "never",
                fail[:100],
            )
        out.print(t)


@host_app.command("remove")
def host_remove(ctx: typer.Context, name: str) -> None:
    """Delete a host that never collected anything (otherwise: `host set NAME --state retired`, which keeps history)."""
    from drivecanary.models import AttrlogCursor, DriveSighting, HostAttempt, Pool, SmartRun

    state = _state(ctx)
    with _factory(state)() as s:
        host = _host(s, name)
        has_data = any(
            s.scalar(select(t.id).where(t.host_id == host.id).limit(1)) is not None
            for t in (DriveSighting, SmartRun, Pool)
        )
        if has_data:
            err.print(
                f"[red]{name} has drive history; retire it instead: drivecanary host set {name} --state retired[/red]"
            )
            raise typer.Exit(2)
        for a in s.scalars(select(HostAttempt).where(HostAttempt.host_id == host.id)):
            s.delete(a)
        for c in s.scalars(select(AttrlogCursor).where(AttrlogCursor.host_id == host.id)):
            s.delete(c)
        s.delete(host)
        s.commit()
        _write_ssh_material(state, s)
    typer.echo(f"removed {name}")


# ============================================================================ collect / import / status


@app.command("collect")
def collect_cmd(
    ctx: typer.Context,
    host: Annotated[list[str] | None, typer.Option("--host", "-H", help="Only these hosts (repeatable).")] = None,
    trigger: Annotated[str, typer.Option("--trigger", help="timer or manual; recorded on the run.")] = "manual",
    parallel: Annotated[int | None, typer.Option("--parallel", help="Override [collect].parallel.")] = None,
) -> None:
    """Pull from every due host (or the ones named) and store what came back. Exit 1 if any host failed."""
    from drivecanary.collect import collect

    state = _state(ctx)
    try:
        run, outcomes = collect(state.config, _factory(state), only=host, trigger=trigger, parallel=parallel)
    except ValueError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None
    for o in outcomes:
        if o.ok and o.result is not None:
            r = o.result
            line = f"{o.name}: ok; {r.runs} drive readings, {r.pools} pools, {r.attrlog_lines} attrlog lines"
            if r.warnings:
                line += "; warnings: " + " | ".join(r.warnings)
            typer.echo(line)
        else:
            typer.echo(f"{o.name}: FAILED ({o.failure_class.value if o.failure_class else '?'}): {o.reason}")
    typer.echo(f"run {run.id}: {run.hosts_ok} ok, {run.hosts_failed} failed of {run.hosts_expected}")
    if run.hosts_failed:
        raise typer.Exit(1)


@import_app.command("attrlog")
def import_attrlog(
    ctx: typer.Context,
    files: Annotated[list[Path], typer.Argument(help="attrlog.MODEL-SERIAL.ata.csv files (the name is the drive).")],
    host: Annotated[str | None, typer.Option("--host", help="The host the files came from (its zone is used).")] = None,
    tz: Annotated[
        str | None,
        typer.Option("--tz", help="Zone of the timestamps (default: the host's, else [collect].default_tz)."),
    ] = None,
) -> None:
    """Import smartd attribute logs copied from a host. Idempotent: lines already stored are skipped."""
    from drivecanary.attrlog import import_file
    from drivecanary.timeutil import zone

    state = _state(ctx)
    cfg = state.config
    with _factory(state)() as s:
        host_row = _host(s, host) if host is not None else None
        z = zone(tz or (host_row.tz if host_row is not None else None) or cfg.collect.default_tz)
        for f in files:
            try:
                drive, res = import_file(s, str(f), tz=z, cfg=cfg.status, host_id=host_row.id if host_row else None)
            except ValueError as e:
                err.print(f"[red]{f}: {e}[/red]")
                raise typer.Exit(2) from None
            s.commit()
            typer.echo(
                f"{f.name}: drive {drive.id} ({drive.model_key} {drive.serial_key}): {res.lines} lines, "
                f"{res.added} added, {res.duplicates} already stored"
            )


@app.command("status")
def status_cmd(ctx: typer.Context) -> None:
    """The page, in the terminal: every drive worst first, then the hosts and pools."""
    from drivecanary import queries

    state = _state(ctx)
    cfg = state.config
    with _factory(state)() as s:
        ov = queries.overview(s, cfg)
    typer.echo(f"overall: {ov.verdict.value.upper()}  " + "  ".join(f"{k} {v}" for k, v in sorted(ov.counts.items())))
    if ov.last_heard:
        typer.echo(
            f"hosts: {ov.hosts_ok} of {ov.hosts_watched} ok, "
            f"the latest heard from {shown(ov.last_heard, cfg.web.timezone)}"
        )
    if ov.last_run:
        typer.echo(
            f"last pull: {shown(ov.last_run.started_at, cfg.web.timezone)}, {ov.last_run.hosts_ok} ok / "
            f"{ov.last_run.hosts_failed} failed of {ov.last_run.hosts_expected} pull hosts"
        )
    t = Table(title="drives")
    for col in ("verdict", "host", "dev", "model", "serial", "temp", "hours", "age", "why"):
        t.add_column(col)
    for d in ov.drives:
        run = d.latest
        t.add_row(
            d.verdict.value,
            d.host.name if d.host else "?",
            d.dev_name or "",
            d.drive.label,
            d.drive.serial or d.drive.serial_key,
            f"{run.temp_c}" if run and run.temp_c is not None else "",
            f"{run.power_on_hours}" if run and run.power_on_hours is not None else "",
            f"{d.age_hours:.1f}h" if d.age_hours is not None else "never",
            "; ".join(d.reasons)[:80],
        )
    out.print(t)
    t = Table(title="hosts")
    for col in ("verdict", "host", "state", "drives", "last success", "last failure"):
        t.add_column(col)
    for h in ov.hosts:
        a = h.last_attempt
        fail = f"{a.failure_class}: {a.reason}" if a and not a.ok and a.failure_class else ""
        t.add_row(
            h.verdict.value,
            h.host.name,
            h.host.state,
            str(h.drives),
            f"{h.age_hours:.1f}h ago" if h.age_hours is not None else "never",
            fail[:80],
        )
    out.print(t)
    if ov.pools:
        t = Table(title="pools")
        for col in ("verdict", "host", "kind", "name", "health", "why"):
            t.add_column(col)
        for p in ov.pools:
            t.add_row(
                p.verdict.value,
                p.host.name,
                p.pool.kind,
                p.pool.name,
                (p.latest.health or "") if p.latest else "",
                (p.latest.reasons or "").replace("\n", "; ")[:80] if p.latest else "",
            )
        out.print(t)


# ============================================================================ web


@web_app.command("serve")
def web_serve(ctx: typer.Context) -> None:
    """Run the page on [web].bind:[web].port (uvicorn)."""
    import uvicorn

    state = _state(ctx)
    cfg = state.config
    if state.config_path is not None:
        os.environ[CONFIG_ENV] = str(state.config_path)
    uvicorn.run(
        "drivecanary.web.app:create_app",
        factory=True,
        host=cfg.web.bind,
        port=cfg.web.port,
        log_level="warning",
        access_log=False,
    )


@ingest_app.command("serve")
def ingest_serve(ctx: typer.Context) -> None:
    """Listen for push agents on [ingest].bind:[ingest].port (uvicorn)."""
    import uvicorn

    state = _state(ctx)
    cfg = state.config
    if state.config_path is not None:
        os.environ[CONFIG_ENV] = str(state.config_path)
    uvicorn.run(
        "drivecanary.ingest_api:create_ingest_app",
        factory=True,
        host=cfg.ingest.bind,
        port=cfg.ingest.port,
        log_level="warning",
        access_log=False,
    )


def run() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "help":
        sys.argv[1] = "--help"
    app()


if __name__ == "__main__":
    run()

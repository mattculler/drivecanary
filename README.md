<p align="center"><img src="docs/logo.jpg" alt="drivecanary: a canary standing on a hard drive platter" width="480"></p>

# drivecanary

Drive health for a home LAN: SMART, pool state and trends from every host, on one page. Once an hour a hub VM
pulls from each host over ssh, or the host's agent pushes to the hub over HTTP; the hub keeps every reading in
one SQLite file and shows what is failing, what is about to, and what has not been heard from.

## How it fits together

- **`docs/design.md`** says how it works and why; what follows is the short version.
- **Pull hosts** run nothing between collections. Each has a `drivecanary` user whose only ssh key is the hub's,
  bound by a forced command to `deploy/host/gate`; the gate runs `deploy/host/probe` as root through one
  sudoers line, and the probe runs `smartctl -j -x` per device, `zpool`/`btrfs`/`mdadm` where present, and
  hands back everything it saw plus the tail of smartd's attribute logs since the hub last read them.
  `deploy/host/install-host.sh HOST` sets all of that up from your workstation.
- **Push hosts** are the exception: a host that is only up on demand, or the hypervisor the hub runs on,
  runs `deploy/host/agent` from a timer instead and reports to the hub with a token. Same probe, same data.
- **The hub** (`drivecanary collect`, from a systemd timer) parses what came back (`smartctl -j` JSON: no text
  scraping), judges every drive (`drivecanary/status.py`: SMART PASSED is not enough; the exit bits,
  Backblaze's five counters and the NVMe health log are), and records for every host what happened, so a
  silent host always has a reason on its row.
- **The page** (`drivecanary web serve`, FastAPI + Jinja2 + uPlot) reads the database and nothing else: the
  overview worst-first, a page per drive with trends, the hosts and their attempts.
- **History** comes from two places: the hourly probe, and smartd's own attrlog CSVs, which Debian has been
  writing every 30 minutes on every host for years (`drivecanary import attrlog` for copies; the collector
  keeps pulling new lines afterwards).

## Quickstart on a VM

The hub is a small Debian VM (1 vCPU and 1 GB are plenty) with a static address. As root on the VM:

```bash
apt install git curl openssh-client
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
git clone <this repository> /opt/drivecanary
/opt/drivecanary/deploy/install.sh        # users, units, venv, database; prints the hub's public key
editor /etc/drivecanary/config.toml       # optional: the defaults suit a LAN; [web].timezone is the zone times are shown in
systemctl restart drivecanary-web         # after changing the config
```

Then add a host. Pull is the default: the hub reaches the host over ssh with a key that can only run the
read-only probe.

```bash
# on your workstation, from a checkout, with root ssh to the host
deploy/host/install-host.sh atlas --hub-ip HUB-IP --hub-key 'ssh-ed25519 AAAA... drivecanary-hub@hub'
# on the hub, with the host key fingerprint install-host.sh printed last
drivecanary host add atlas --address atlas.domain --fingerprint SHA256:...
drivecanary collect --host atlas
```

For a host the hub should not reach (its own hypervisor, a router, a machine that is only up now and then),
use push: `drivecanary host add hv --transport push` on the hub prints a token, and
`deploy/host/install-host.sh hv --push --hub-url http://HUB-IP:8081 --token TOKEN` on the workstation
installs the agent. Add `--self-tests` to either install to give every drive a monthly short and yearly long
self-test, each on a date of its own.

The page is at `http://HUB-IP:8080/`; its hosts page repeats these steps with your hub's address and key
filled in. `deploy/README.md` has the rest: backups, updates (`drivecanary update`), what each failure on a
host's row means, OPNsense.

## Development quickstart

Requirements: Linux, Python 3.13+, `uv`. The tests run the real host scripts against stand-ins, so they need
`sh`, `flock` and `timeout` too, as any Linux has. Nothing needs root or the network.

```bash
git clone <this repository> && cd drivecanary
uv sync                                  # .venv with every dependency
make check                               # ruff, mypy --strict and the test suite

make dev-config                          # dev/config.toml: database, keys and backups under ./dev (gitignored)
export DRIVECANARY_CONFIG=dev/config.toml
make dev-db                              # create the database schema
uv run drivecanary config check

# history to look at: a host's smartd attribute logs, copied from /var/lib/smartmontools on it
uv run drivecanary host add atlas --address atlas.domain --no-keyscan
uv run drivecanary import attrlog path/to/attrlog.*.csv --host atlas

uv run drivecanary web serve             # http://127.0.0.1:8080/
uv run drivecanary status                # the same, in the terminal
```

To collect from a real host from a dev checkout, give it a hub key
(`ssh-keygen -t ed25519 -N '' -f dev/ssh/id_ed25519`), install the host with that public key and your
workstation's address as `--hub-ip`, pin its key (`uv run drivecanary host keyscan atlas --fingerprint
SHA256:...`), and run `uv run drivecanary collect --host atlas`.

Names, addresses and serials in examples and tests are placeholders (`atlas`, `hv`, `10.100.100.x`,
`HOST.domain`); your own belong in the unversioned config that holds them (`/etc/drivecanary/config.toml` on
the hub, `~/.config/drivecanary/hosts/` on the workstation).

Everything in the page and the CLI works offline on imported attrlogs; the first real collection adds the
identity smartctl knows (model family, firmware, WWN, capacity), the pools, and NVMe drives. `scripts/check-private`
refuses a commit that contains any of your own names, addresses or serials, going by a list that is never
committed either (`scripts/check-private --help` says where it lives and how to install it as a git hook).

## Layout

- `src/drivecanary/config.py` — the settings model; `deploy/config.example.toml` is generated from it
  (`make config-example`) and a test fails when it is stale.
- `src/drivecanary/models.py` — SQLAlchemy models; `alembic/` — migrations (`drivecanary db migrate`).
- `src/drivecanary/smart.py` — `smartctl -j` into a flat report; `status.py` — the OK/WARN/FAIL rules.
- `src/drivecanary/envelope.py` — the byte stream the gate sends; `ingest.py` — that stream into rows;
  `collect.py` — the ssh pull, parallel per host, one transaction per host.
- `src/drivecanary/push.py` — what a push agent delivers, stored the same way; `ingest_api.py` — the
  listener it delivers to, a service of its own so the page stays read-only.
- `src/drivecanary/pools.py` — which drives a pool is made of: what its status names, found in what the
  host reports.
- `src/drivecanary/attrlog.py` — smartd attribute logs, whole files or chunks past a cursor.
- `src/drivecanary/queries.py` — what the page and `drivecanary status` show; `web/` — the page.
- `deploy/` — the VM install (`install.sh`, `update.sh`, `backup.sh`, units) and `deploy/host/` — what goes
  on each monitored host (`probe`, `gate`, `agent`, `sudoers`, `install-host.sh`). See `deploy/README.md`.
- `scripts/make_icons.py` — cuts the page's icons from `docs/logo.jpg` (run by hand when the logo changes).
- `scripts/check-private` — keeps the names, addresses and serials of your own setup out of commits.
- `tests/fixtures/smartctl/` — real `smartctl -j` captures (from Scrutiny's test data, MIT) covering ATA,
  SATA SSD, NVMe, SAS, a USB bridge, a failing drive and an open failure.

## Not yet

NVMe attribute logs and scheduled NVMe self-tests (smartd does both only from smartmontools 7.5), and hosts
other than Debian and OPNsense. Alerting is smartd's job on each host (`-M exec`), not the hub's.

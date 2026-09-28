# Deploying drivecanary

Target: a Debian VM, native install, systemd. No Docker.

```bash
sudo apt install git curl openssh-client
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh   # system-wide: a ~/.local/bin uv is invisible under sudo
sudo git clone <repo> /opt/drivecanary
sudo /opt/drivecanary/deploy/install.sh
sudo -e /etc/drivecanary/config.toml    # [web].bind, [collect] timeouts; the defaults are fine on a LAN
sudo systemctl restart drivecanary-web
```

Give the VM a static IP (or a DHCP reservation): every host's `from=` clause names it.

`install.sh` makes two system users: `drivecanary` runs the collector and owns the database and the hub's
ssh key in `/var/lib/drivecanary/ssh`; `drivecanary-web` runs the page, shares the `drivecanary` group for
the database, and cannot read the key. It prints the hub's public key at the end; that is what every host
gets.

What runs when (`systemctl list-timers 'drivecanary-*'`):

| unit | schedule | does |
|---|---|---|
| `drivecanary-web.service` | always | the page on `[web].bind:port` |
| `drivecanary-collect.timer` | hourly | ssh to every due host, ingest, judge; a host that failed is that host's row, not a failed unit |
| `drivecanary-backup.timer` | 02:30 | one `tar.gz` a night in `/var/lib/drivecanary/backups` (keeps 14): a snapshot of the database, `config.toml`, the host list, a manifest |

Logs are JSON lines in journald: `journalctl -u 'drivecanary-*' -f`. The CLI from any admin shell:
`drivecanary status`, `drivecanary host list`, `drivecanary collect --host atlas` (passwordless with
`deploy/sudoers.example`). Update: `drivecanary update` (pull, stop the jobs, sync, migrate, restart web,
start the timers), or `make update` from the checkout.

## Adding a host

Three steps, the first from your workstation over your own ssh (root on the host), the other two on the hub.

```bash
# 1. workstation: the drivecanary user, the probe and gate, the sudoers line, the hub's key bound to the gate
deploy/host/install-host.sh atlas --hub-ip 10.100.100.NN --hub-key 'ssh-ed25519 AAAA... drivecanary-hub@hub'
#    (--target root@atlas.domain if your ssh config does not already say so)
#    It ends by printing the host's ed25519 fingerprint.

# 2. hub: the row and the pinned host key (--tz only if the host is not in [collect].default_tz)
drivecanary host add atlas --address atlas.domain --fingerprint SHA256:<from step 1>

# 3. hub: the first collection; the host goes from pending to ok, and its attrlogs are pulled from the start
drivecanary collect --host atlas
```

`install-host.sh` is idempotent and is also how a probe upgrade or a repair is rolled out: run it again.
What it needs on the host: Debian bullseye or newer (`smartctl` 7.0+ for `--json`), `sudo` (installed if
absent), and if sshd has `AllowUsers`/`AllowGroups`, `drivecanary` added to it.

What the hub's key can do on a host: run the gate. The gate runs the probe through one sudoers line
(`/usr/local/lib/drivecanary/probe ""`, no arguments) and reads the attrlog files; it refuses a shell, sftp
and any other command with exit 64. A key that is *not* restricted shows up as `key_not_restricted` on the
host's row and is not used again until it is.

HBAs and odd USB bridges: `/etc/drivecanary/devices.conf` on the host overrides a device's `-d` type or
skips it (`deploy/host/devices.conf.example`).

### When a host is quiet

Every attempt is on the host's page with a class and a reason:

| class | means | fix |
|---|---|---|
| `unreachable` | ssh could not connect (down, no route, DNS) | nothing; it is retried every hour, then every 6 h after a day |
| `timeout` | connected, but the probe did not finish in `[collect].host_timeout_seconds` | a hung device; `devices.conf` to skip it, or raise the timeout |
| `auth` | the key was refused | re-run `install-host.sh` (the authorized_keys line), check `AllowUsers` |
| `hostkey` | the host's key changed | `drivecanary host keyscan atlas --fingerprint SHA256:...` after checking it really was reinstalled |
| `key_not_restricted` | the key ran the verb as a shell command | re-run `install-host.sh`: the `command=` clause is missing |
| `sudo` | the gate could not run the probe as root | re-run `install-host.sh` (the sudoers drop-in) |
| `probe` | the probe crashed | its stderr is in the reason; `sudo -u drivecanary sudo -n /usr/local/lib/drivecanary/probe` on the host |
| `envelope` | the gate's output was cut short or malformed | a version mismatch: re-run `install-host.sh` and `drivecanary update` |

A host that is away for months: `drivecanary host set NAME --state paused` stops the attempts (and the
counting); `--state pending` re-arms it. `--state retired` keeps its history and takes it off the page.

## smartd on the hosts

Debian's default `smartd.conf` (`DEVICESCAN -d removable -n standby -m root -M exec ...`) writes the attribute
logs drivecanary reads, but schedules **no self-tests** and its alert mail goes nowhere without a mailer.
Alerting is smartd's job (it runs every 30 minutes, whether or not the hub is up), so on each host consider:

```
DEVICESCAN -a -o on -S on -n standby,q -s (S/../.././02|L/../../6/03) -W 4,45,55 -m <nomailer> -M exec /usr/local/bin/smartd-notify
```

with `smartd-notify` a few lines that post `$SMARTD_MESSAGE` to ntfy or whatever you read. `smartd -q
showtests` confirms the schedule parses. The hub does not alert.

## Backups

`drivecanary-backup.timer` builds one bundle a night in `/var/lib/drivecanary/backups` and keeps 14. For the
off-box copy on the NAS share, a drop-in the update never touches:

```bash
sudo systemctl edit drivecanary-backup.service
```
```ini
[Service]
Environment=BACKUP_COPY_DIR=/mnt/nas/backups/drivecanary
```

The share must be mounted before the timer fires (`remote-fs.target`; `After=` is set); the directory is
never created by the script, so an unmounted share fails the copy loudly after the local bundle is safe. The
share keeps the newest 30, the last of each of the last 12 months, and the last of every older year, like
gamefinder's. The database snapshot inside is taken with SQLite's backup API and switched to rollback
journal mode: it is safe to open where it sits.

Restore: stop the units, `gunzip` the snapshot to `[db].path`, remove any old `-wal`/`-shm` beside it,
`config.toml` to `/etc/drivecanary/`, `drivecanary db migrate` if the code is newer, start the units.
The live database stays on local disk: WAL mode does not work over NFS.

## Bringing history in by hand

Copies of `/var/lib/smartmontools/attrlog.*.csv` from a host import with
`drivecanary import attrlog FILE... --host NAME` (the host's zone converts the local timestamps). It is
idempotent, and the collector's own pulls of the same files do not duplicate a line. NVMe attrlogs exist only
from smartmontools 7.5; ATA and SCSI logs are read, NVMe ones are not yet.

## Later: a push agent

For a host that is only up on demand (wake-on-LAN), or one that should not hold an inbound key (the
hypervisor the hub runs on), the same probe can run from a timer on the host and POST its envelope to the
hub. The database already records what that needs; the hub-side endpoint and the agent are not written.
`docs/transport-design-2026-09-28.md` §2 is the design.

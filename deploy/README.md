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
| `drivecanary-ingest.service` | always | where push agents deliver, on `[ingest].bind:port` (8081) |
| `drivecanary-collect.timer` | hourly | ssh to every due host, ingest, judge; a host that failed is that host's row, not a failed unit |
| `drivecanary-backup.timer` | 02:30 | one `tar.gz` a night in `/var/lib/drivecanary/backups` (keeps 14): a snapshot of the database, `config.toml`, the host list, a manifest |

Logs are JSON lines in journald: `journalctl -u 'drivecanary-*' -f`. The CLI from any admin shell:
`drivecanary status`, `drivecanary host list`, `drivecanary collect --host atlas` (passwordless with
`deploy/sudoers.example`). Update: `drivecanary update` (pull, stop the jobs, sync, migrate, restart web,
start the timers), or `make update` from the checkout. It says which commits it brought.

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
It keeps what each host was given in `~/.config/drivecanary/hosts/HOST` on the workstation (mode 0600: a
push host's token is in it), so that running it again is `deploy/host/install-host.sh atlas`, and every
host at once is `deploy/host/install-host.sh --all`. Options given on a later run override what was kept.
What it needs on the host: Debian bullseye or newer (`smartctl` 7.0+ for `--json`), `sudo` (installed if
absent), and if sshd has `AllowUsers`/`AllowGroups`, `drivecanary` added to it.

What the hub's key can do on a host: run the gate. The gate runs the probe through one sudoers line
(`/usr/local/lib/drivecanary/probe ""`, no arguments) and reads the attrlog files; it refuses a shell, sftp
and any other command with exit 64. A key that is *not* restricted shows up as `key_not_restricted` on the
host's row and is not used again until it is.

`install-host.sh` is also how a host gets a newer probe, gate or agent. A host's page says which versions it
runs and says so when the checkout on the hub has newer ones. Nothing on the hub needs doing again after a
reinstall: the host says what it runs with its next report. After `drivecanary update` on the hub, from the
same checkout on the workstation: `git pull && deploy/host/install-host.sh --all`.

Times on the pages are in `[web].timezone` (America/New_York unless you say otherwise): EST or EDT, as the
date has it. The database keeps UTC.

A host's page is at `/host/NAME` (`http://hub:8080/host/atlas`), so another site can link to it knowing only
the name. Its address can be changed with `drivecanary host set NAME --address atlas.domain`; for a pull host
the pinned host key moves to the new address with it.

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
Alerting is smartd's job (it runs every 30 minutes, whether or not the hub is up), so on each host consider
`-W 4,45,55 -m <nomailer> -M exec /usr/local/bin/smartd-notify` on the `DEVICESCAN` line, with `smartd-notify`
a few lines that post `$SMARTD_MESSAGE` to ntfy or whatever you read. The hub does not alert.

### Self-tests

`install-host.sh HOST ... --self-tests` gives every SATA and SAS drive on the host a schedule of its own, and
`--no-self-tests` takes it out again. It is smartd that starts the tests; the hub never does, and the probe
stays read-only.

| test | when | how long |
|---|---|---|
| short | the 17th of every month, each drive at an hour of its own (01:00, 02:00, ...) | a minute or two |
| long | once a year at 01:00, each drive on a date of its own: the 19th or the 24th of a month of its own | hours; a 14 TB drive says a day |

So no two drives test at once, and two long tests are never less than five days apart: drive 1 has
19 January, drive 2 19 July, drive 3 19 April, drive 4 19 October, and the 24ths follow once the 19ths are
taken (24 drives to a host). Nothing starts before the 17th, which keeps a test clear of the scrubs that run
by the calendar: Debian's md check on the first Sunday of a month and its ZFS scrub on the second, neither of
which can fall after the 14th.

What it does on the host: writes one line a drive, named by `/dev/disk/by-id` so that a drive keeps its dates
when its letter changes, between `# BEGIN drivecanary self-tests` and `# END drivecanary self-tests` above the
`DEVICESCAN` line of `/etc/smartd.conf`. Each line carries `DEVICESCAN`'s own directives, so a drive is
watched and mailed about as before; `DEVICESCAN` goes on covering whatever is not named. The new file is put
to `smartd -q showtests` before it replaces the old, which is kept as `smartd.conf.before-drivecanary`.
`/etc/drivecanary/selftests` remembers which dates a drive has, so that adding a drive moves no other.
`/usr/local/lib/drivecanary/selftests show` prints what it would write and changes nothing.

What it cannot do:

- **A scrub started by hand, or by a timer of your own, is not seen.** smartd looks at the clock and at the
  drive it is about to test, nothing else. A btrfs scrub that takes two days should be started before the
  17th or after the 25th, or in a month none of that pool's drives has its long test in (the host's page
  lists them).
- **A host that was off at the hour runs the test when it is next up**, which is then at no planned time.
- **NVMe drives are not scheduled**: smartd starts their self-tests only from smartmontools 7.5.
- A `smartd.conf` without a `DEVICESCAN` line is one you wrote, and is left alone: the script says so.
- On OPNsense there is no smartd, so root's cron starts the tests (`/usr/local/etc/cron.d/drivecanary-selftests`),
  on the same dates.

A host's page lists each drive's next short and long test, how long the drive says a long test takes, and
the last test it ran. A drive with a schedule and no test in 45 days of power-on time is a warning
(`[status].selftest_max_age_days`; 0 turns it off): the schedule has stopped. A drive that is testing, and a
pool that is scrubbing, say so beside their names on every page.

## What a drive's page goes by

Attribute names are smartctl's, from its drive database on the host that read the drive: what an id means is
the vendor's to decide (233 is a wearout indicator on Intel and gigabytes written on WD). A drive that
database does not know gets generic names, and its page says so; `update-smart-drivedb` on the host, or a
newer smartmontools, may know it. Nothing new is read from the drive by that: the drive reports what it
reports, and the database only says what it means.

Wear on a SATA SSD is charted two ways: the standard figure (the Percentage Used Endurance Indicator of the
ATA device statistics, which means the same on every vendor's drive, and warns at
`[status].nvme_percentage_used_warn` like NVMe's), and the vendor's own attributes, found by name.

The ATA error log is a WARN only for entries from the last `[status].error_log_recent_hours` power-on hours,
and only for the two kinds that mean something: a read or addressing error (the drive), and a CRC error (the
cable, the backplane or the controller). An aborted command is usually one the drive does not support.

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

## Push hosts

Some hosts should report to the hub instead of being reached by it: one that is only up on demand (a NAS woken
over the LAN), and the hypervisor this VM runs on, which should accept no key held by one of its own guests.
On those an agent runs from a systemd timer, hourly and two minutes after boot, and POSTs to the hub's ingest
listener (`drivecanary-ingest.service`, port 8081) with the host's token. It sends exactly what a pull would
have fetched, and the hub stores it the same way.

```bash
# 1. hub: the row. It prints the host's token, once, and the command for step 2.
drivecanary host add pve --transport push

# 2. workstation: the drivecanary user, the probe, the agent with its token, and its timer
deploy/host/install-host.sh pve --push --hub-url http://10.100.100.NN:8081 --token <from step 1>
```

Step 2 ends by sending the first report and showing the hub's answer. After that `drivecanary status` and the
host's page show it like any other host, with `push` in the "how" column.

What is on the host: the same probe, gate and sudoers line as for a pull; `/usr/local/lib/drivecanary/agent`;
`/etc/drivecanary/agent.conf` (the hub's URL) and `/etc/drivecanary/agent.token` (mode 0600); the spool and
the agent's state in `/var/lib/drivecanary-agent`. No `authorized_keys`: nothing on a push host accepts a
connection from the hub.

When the hub is away the agent keeps what it collected (the newest 24 reports, 256 MB at most) and delivers
it, oldest first, when the hub is back. Its failures in between are told to the hub with the first delivery
that gets through, so the host's page says why there was a gap. On the host: `journalctl -u drivecanary-agent`.

A push host that has gone quiet shows STALE after `[collect].stale_after_hours`. The hub cannot say why (it
never reaches out to a push host); the host's journal can.

| | |
|---|---|
| a lost or leaked token | `drivecanary host token pve`, then step 2 again with the new one |
| pull host to push | `drivecanary host set atlas --transport push`, then `install-host.sh atlas --push --hub-url ... --token ...` (it removes the hub's key) |
| push host to pull | `drivecanary host set pve --transport pull`, then `install-host.sh pve --pull --hub-ip ... --hub-key ...` (it stops the agent), then `drivecanary host keyscan pve` |
| a host that is gone | `drivecanary host set NAME --state retired`, and `install-host.sh NAME --forget` on the workstation |

### OPNsense

A router running OPNsense (FreeBSD) is a push host, by the same two steps:

```bash
drivecanary host add opnsense --transport push                                   # hub
deploy/host/install-host.sh opnsense --push --hub-url http://10.100.100.NN:8081 \
  --token <from the hub> --target root@opnsense.domain                           # workstation
```

Before that, on the router: the `os-smart` plugin (System > Firmware > Plugins), which brings `smartctl`, and
ssh for root (System > Settings > Administration), which the install needs and the agent does not.

It is push only, and the agent runs as root from `/usr/local/etc/cron.d/drivecanary`, hourly and two minutes
after a boot. OPNsense deletes accounts it did not make itself and lets only administrators in over ssh, so
there is no unprivileged user to give the job to and no restricted key to pull with; its own crontab names
that directory as the place for jobs of yours. What the agent says goes to the system log (System > Log Files
> General, `drivecanary-agent`). The files: `/usr/local/lib/drivecanary/` (probe, gate, agent),
`/usr/local/etc/drivecanary/` (the hub's URL, the token), `/var/db/drivecanary-agent/` (the spool).

What is different about what it reports: no attribute-log history (smartd does not run there, so trends start
at the first report), no pools on a UFS install, and the router's zone is read from OPNsense's own config.
After a firmware upgrade, check the host still reports; if the upgrade removed the files, run the install again.

The token is the only thing between the LAN and that host's row: anyone holding it can post readings as that
host, and nothing else. The hub keeps only its hash. The listener reads no more than `[ingest].max_body_mb`
and inflates no further than `[collect].max_output_mb`.

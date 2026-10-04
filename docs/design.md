# How drivecanary works, and why

drivecanary watches the drives of a handful of machines on a home LAN: SMART, pool state and long-term
trends, on one page. This is the reasoning behind its shape. For how to install and run it, see
`deploy/README.md`.

## What it is for, and what it is not

- **For:** knowing which drive is failing, which is about to, and which machine has gone quiet, with the
  history to see a trend coming: years of readings per drive, kept in one SQLite file.
- **Not for alerting.** smartd on each host already watches its own drives every 30 minutes and can run a
  command when something changes (`-M exec`). That keeps working while the hub is down, and the hub is often
  a VM on one of the machines it watches. drivecanary is where you look, not what wakes you.
- **Not a fleet tool.** It assumes a few hosts, static addresses, and an admin who already reaches every
  host over ssh as root.

## One probe, two ways to deliver it

Everything a host reports comes from one root-owned shell script, `deploy/host/probe`, the same on every
host and in both delivery modes:

- It takes no arguments and reads no stdin or environment, so nothing a requester sends can reach root.
- It runs a fixed list of read-only commands, each under `timeout`, and holds a `flock -n` so a wedged run
  makes the next one answer "busy" instead of piling up:
  - `smartctl --scan-open -j`, then per device `smartctl -j -x -n standby,3 -d TYPE`. Sleeping disks stay
    asleep (`-n standby` with a distinct exit status, and `-d` given so type detection cannot spin one up).
  - `zpool status` and `zpool list`, `btrfs device stats` and `btrfs scrub status` per btrfs mount,
    `/proc/mdstat` and `mdadm --detail`, `lsblk -J`, os-release, the host's time zone, and the smartd
    schedule in force.
- It parses nothing. Each command's stdout, stderr and exit status go out as a length-prefixed frame, and
  the hub does all the parsing. A smartctl that changes its JSON between versions is handled by updating
  the hub, not every host.
- It is POSIX sh, because Python on every host (or on a router) is not a given. It runs on Debian and on
  FreeBSD (OPNsense).

### The privilege boundary is one sudoers line

```
drivecanary ALL=(root) NOPASSWD: /usr/local/lib/drivecanary/probe ""
```

The `""` forbids arguments, and sudo's `env_reset` strips the environment. Never grant `smartctl` itself,
not even `smartctl *`: as root it can freeze a drive's security state, disable SMART, start or abort tests,
and write logs. Some monitoring tools document exactly that rule. The probe starts no self-test either;
that is smartd's job (see "Self-tests" below).

The exception is OPNsense, which keeps no account it did not create and lets only administrators in over
ssh. There the probe and agent run from root's cron, with no user, no sudo and no key; such a host can only
push.

### Pull (the default)

The hub runs `ssh HOST drivecanary-collect [FILE=OFFSET ...]` with system OpenSSH and a config it generates
(`-F`), so nothing from the hub's own `/etc/ssh` leaks in. On the host:

- The hub's key sits in the `drivecanary` user's authorized_keys as
  `restrict,from="HUB-IP",command="/usr/local/lib/drivecanary/gate"`. It works only from the hub's address,
  and only to run the gate. The hub needs a static address.
- The gate (`deploy/host/gate`, unprivileged) accepts one verb. It runs the probe through sudo, then
  appends smartd's attribute logs from the byte offsets the hub asks for. File names and offsets are
  checked against strict patterns. Anything else, an interactive shell or sftp included, exits 64.
- A compromised hub can therefore make every host run the read-only probe, and nothing more.
- If a host ever answers as if the key were not restricted (the verb run as a shell command), the hub
  marks it `key_not_restricted` and stops using it until it is fixed.

Why pull is the default:

- **Silence needs explaining.** Hosts on a home LAN are off for days or months. Pull records *why* a host
  is quiet on every attempt: `unreachable`, `auth`, `hostkey`, `sudo`, `probe`, `timeout`, `envelope`,
  `too_large`. Push can only say that it is quiet.
- **No secret on any host and no listening service** beyond the ssh you already run.
- **One run, one clock.** Every collection is stamped by the hub, and each host's own clock is recorded
  beside it, which measures skew for free.
- **Upgrades are one command** from the workstation: `install-host.sh` is idempotent and also the repair
  path. The hub's key can never install anything.

Pull's weakness is gaps while the hub is down. ATA history refills itself, because smartd's attribute logs
are read from where the hub left off. NVMe and pool state for the outage are lost.

### Push (for hosts the hub should not reach)

Some hosts should not accept a key from the hub at all. The hypervisor the hub VM runs on is one: a guest
holding a credential to its own host is a poor trade, even a restricted one. Others are machines that are
only up on demand, and routers that keep no account they did not make.

- An agent (`deploy/host/agent`, from a systemd timer or root's cron) has the gate build the same
  envelope, gzips it, spools it, and POSTs it to the hub's ingest listener with a per-host bearer token.
- The hub keeps only a hash of each token. A stolen token lets someone forge one host's readings, nothing
  else. The listener reads a bounded body and inflates it no further than a cap.
- The spool survives the hub being away: nothing is lost up to the spool's limits, and the agent reports
  its failed deliveries with its next successful one.
- Each payload carries an id, so a retry of something already stored is acknowledged and not stored twice.
- Attribute-log offsets travel back in the hub's reply, so the agent sends the next chunk from where the
  hub's copy ends.

Both modes feed the same ingest code. A host can move between them by running the install the other way,
and the hub can have hosts of both kinds at once.

## On the hub

- **Two users.** The collector owns the ssh key and the database. The web page runs as another user that
  can read the database and cannot read the key.
- **One SQLite file in WAL mode, on local disk.** WAL does not work over NFS. Backups take a snapshot
  through SQLite's backup API, switch the copy to rollback-journal mode so it opens where it sits, and copy
  it to a NAS share if one is set, keeping a rolling set (newest 30, monthly for a year, yearly after).
- **Every reading is kept.** Each smartctl reading keeps its full JSON, with the attributes broken out per
  sample for trends. A drive is identified by model and serial, so it keeps its history when it moves
  between hosts or device names.
- **Hosts have states:** `pending` (never collected), `ok`, `unreachable` (retried, backing off after a
  day), `broken` (reached, but auth, sudo or the probe is wrong), `paused` and `retired` (history kept).
  Each attempt records its failure class and reason.

## Judging a drive

SMART's overall PASSED is necessary, not sufficient: a drive can report PASSED with media errors logged.
A reading is FAIL or WARN on any of:

- smartctl's exit-status bits (failing now, prefail attribute at threshold, past threshold, error-log and
  self-test-log entries);
- Backblaze's five predictive counters (5, 187, 188, 197, 198) above zero;
- the NVMe health log (critical warning bits, spare below threshold, media errors, percentage used);
- the endurance figure SATA SSDs report;
- recent read or interface errors in the ATA error log (old ones and aborted commands do not count);
- temperature at or over a limit;
- a self-test schedule whose tests have stopped coming.

Every threshold is in the config file. A drive not heard from within `stale_after_hours` is STALE,
whatever it last said.

## Time

- smartd's attribute logs are in each host's local time. They are read in the host's own zone (reported by
  the probe, or set on the host's row, or `[collect].default_tz`). The repeated hour of a DST fall-back is
  resolved by keeping time moving forward; a clock that truly stepped backwards is stored as it was read.
- Everything is stored in UTC and shown in one zone, `[web].timezone`.

## Self-tests

The hub never starts a self-test; smartd does, on a schedule `install-host.sh --self-tests` writes:

- **Short test:** every month on the 17th, each drive at an hour of its own.
- **Long test:** once a year at 01:00, on the 19th or the 24th of a month of the drive's own, so no two
  long tests on one host are less than five days apart.
- Nothing starts before the 17th, which keeps tests clear of the scrubs Debian schedules by the calendar
  (the md check on the first Sunday, ZFS's scrub on the second).
- smartd only checks whether the same drive is already testing. It cannot see a scrub started by hand, so
  that limit is documented rather than hidden.
- Drives are named by `/dev/disk/by-id` and keep their dates when others are added.

## What stays out of the repository

Examples and tests use placeholders (`atlas`, `hv`, `10.100.100.x`, `HOST.domain`). Real host names,
addresses, serials and accounts live in unversioned files: the hub's config file, and the per-host install
records on the workstation. `scripts/check-private` checks commits against your own list of what not to
publish.

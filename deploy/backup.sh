#!/usr/bin/env bash
# Nightly backup bundle: one drivecanary-<stamp>.tar.gz in /var/lib/drivecanary/backups, the newest BACKUP_KEEP
# (default 14) kept. The NAS copy is the second line of defence; this is the first.
#
# In the bundle:
#   drivecanary.db.gz  a snapshot of the live database taken with SQLite's backup API (never a plain copy: a
#                      WAL database copied file by file loses rows), integrity-checked, switched to rollback
#                      journal mode so it can sit on a share without growing -wal/-shm files, then gzipped
#   config.toml        the config the app was running with
#   hosts.txt          the host table as `drivecanary host list` prints it, readable on its own
#   MANIFEST.txt       when, which commit, which schema revision, the sizes, and how to restore
# The bundle is 0600.
#
# BACKUP_COPY_DIR, when set, is the off-box copy: a directory on a NAS share. The bundle is always built and kept
# on this disk first, then copied there and compared byte for byte. What the share keeps is a rolling window:
# the newest BACKUP_COPY_KEEP (default 30, a month of nights), plus the last bundle of each of the
# BACKUP_COPY_MONTHS (default 12) most recent months, plus, unless BACKUP_COPY_YEARLY=0, the last bundle of
# every year older than that (the same window as gamefinder's backups).
# That directory is NEVER created here: if the share is not mounted the path does not exist, and making it would
# write the "off-box" copy onto this very disk, under the mountpoint, where the next mount hides it. A missing or
# unwritable copy dir fails the run AFTER the local bundle is safe. Set these in a drop-in
# (`systemctl edit drivecanary-backup.service`), see deploy/README.md.
set -euo pipefail
case "${1:-}" in help|--help|-h) echo "usage: $0  -- one tar.gz (database snapshot, config.toml, hosts.txt, a manifest) into \${BACKUP_DIR:-/var/lib/drivecanary/backups}, keep \${BACKUP_KEEP:-14}; then a verified copy into \$BACKUP_COPY_DIR when set (an existing directory on a NAS share), where the newest \${BACKUP_COPY_KEEP:-30} are kept, plus the last of each of the \${BACKUP_COPY_MONTHS:-12} most recent months, plus the last of each older year unless BACKUP_COPY_YEARLY=0"; exit 0 ;; esac
DEST=${BACKUP_DIR:-/var/lib/drivecanary/backups}
KEEP=${BACKUP_KEEP:-14}
COPY=${BACKUP_COPY_DIR:-}
COPY_KEEP=${BACKUP_COPY_KEEP:-30}
COPY_MONTHS=${BACKUP_COPY_MONTHS:-12}
COPY_YEARLY=${BACKUP_COPY_YEARLY:-1}
DC=${DRIVECANARY_BIN:-/opt/drivecanary/.venv/bin/drivecanary}
CONFIG=${DRIVECANARY_CONFIG:-/etc/drivecanary/config.toml}
umask 077
mkdir -p "$DEST"
# BACKUP_NOW (YYYYMMDD-HHMM) stands in for the clock, so a test can name the bundle it makes
stamp=${BACKUP_NOW:-$(date +%Y%m%d-%H%M)}

prune_rolling() {
  # $1 directory, $2 newest to keep, $3 months to keep a last bundle of, $4 keep a last bundle of older years (1/0).
  # Names sort by date, so newest first is `sort -r`, and the first bundle met of a month or a year is the last
  # one made in it. No clock is consulted: the window rolls with the bundles themselves.
  local dir=$1 keep=$2 months=$3 yearly=$4 rank=0 nmonths=0 path name y ym keeper
  declare -A month_seen=() year_seen=()
  while IFS= read -r path; do
    name=${path##*/}
    [[ $name =~ ^drivecanary-([0-9]{4})([0-9]{2})[0-9]{2}-[0-9]{4}\.tar\.gz$ ]] || continue
    y=${BASH_REMATCH[1]} ym="${BASH_REMATCH[1]}-${BASH_REMATCH[2]}"
    rank=$((rank + 1)) keeper=0
    if [ -z "${month_seen[$ym]:-}" ]; then
      month_seen[$ym]=1 nmonths=$((nmonths + 1))
      if [ "$nmonths" -le "$months" ]; then keeper=1; fi
    fi
    if [ -z "${year_seen[$y]:-}" ]; then
      year_seen[$y]=1
      if [ "$yearly" = 1 ]; then keeper=1; fi
    fi
    if [ "$keeper" = 0 ] && [ "$rank" -gt "$keep" ]; then rm -f -- "$path"; fi
  done < <(ls -1 "$dir"/drivecanary-*.tar.gz 2>/dev/null | sort -r)
}

work="$DEST/.bundle-$stamp"
rm -rf "$work"
mkdir "$work"
trap 'rm -rf "$work"' EXIT

"$DC" db snapshot "$work/drivecanary.db" >/dev/null
gzip -1 "$work/drivecanary.db"
cp "$CONFIG" "$work/config.toml"
"$DC" host list > "$work/hosts.txt" 2>/dev/null || true
{
  echo "drivecanary backup $stamp ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
  echo "host: $(hostname)"
  echo "version: $("$DC" --version 2>/dev/null | tail -1)"
  echo "schema: $("$DC" db current 2>/dev/null | grep -oE '[0-9a-f]{12}( \(head\))?' | head -1)"
  echo "config: $CONFIG"
  echo "restore: tar -xzf <bundle>; gunzip drivecanary.db.gz; stop the units; put drivecanary.db at [db].path"
  echo "         (remove any old -wal/-shm beside it); config.toml to /etc/drivecanary/; then 'drivecanary db migrate'"
  echo "         if the code is newer than the schema above; start the units"
} > "$work/MANIFEST.txt"
(cd "$work" && du -h -- * | sed 's/^/  /') >> "$work/MANIFEST.txt"

tar -C "$work" -czf "$DEST/drivecanary-$stamp.tar.gz.tmp" .
mv "$DEST/drivecanary-$stamp.tar.gz.tmp" "$DEST/drivecanary-$stamp.tar.gz"
{ ls -1t "$DEST"/drivecanary-*.tar.gz 2>/dev/null || true; } | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "backup: $DEST/drivecanary-$stamp.tar.gz ($(du -h "$DEST/drivecanary-$stamp.tar.gz" | cut -f1))"

if [ -n "$COPY" ]; then
  bundle="drivecanary-$stamp.tar.gz"
  if [ ! -d "$COPY" ]; then
    echo "backup: NOT copied off this disk: $COPY does not exist. Is the share mounted? (It is never created here: that would put the copy on this disk, under the mountpoint.) The local bundle is safe." >&2
    exit 1
  fi
  if ! cp "$DEST/$bundle" "$COPY/.$bundle.tmp" || ! cmp -s "$DEST/$bundle" "$COPY/.$bundle.tmp"; then
    rm -f "$COPY/.$bundle.tmp" || true
    echo "backup: NOT copied off this disk: writing to $COPY failed or the copy read back different (permissions for $(id -un) on the share? space?). The local bundle is safe." >&2
    exit 1
  fi
  mv "$COPY/.$bundle.tmp" "$COPY/$bundle"
  prune_rolling "$COPY" "$COPY_KEEP" "$COPY_MONTHS" "$COPY_YEARLY"
  echo "backup: copied to $COPY/$bundle and read back identical"
fi

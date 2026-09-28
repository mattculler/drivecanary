#!/usr/bin/env bash
# Update a running install: pull, then re-execute from the pulled copy of this script to stop the jobs,
# sync the venv, install the units and the CLI, migrate, restart the web UI and start the timers again.
# Run as root: sudo deploy/update.sh
#
# Two stages because the pull replaces this very file: bash keeps reading the copy it opened, so a
# single-stage script would finish the update with LAST release's steps.
set -euo pipefail
APP=/opt/drivecanary
ETC=/etc/drivecanary
case "${1:-}" in help|--help|-h) echo "usage: sudo $0  -- pull, stop the jobs, sync the venv, install units and CLI, migrate, restart the web UI, start the timers"; exit 0 ;; esac
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo deploy/update.sh)"; exit 1; }
if [ -z "${DRIVECANARY_UPDATE_STAGE2:-}" ]; then
  sudo -u drivecanary -H git -C "$APP" pull --ff-only
  DRIVECANARY_UPDATE_STAGE2=1 exec "$APP/deploy/update.sh" "$@"
fi
UV=/usr/local/bin/uv  # install.sh put it there; a per-user uv is not on sudo's PATH
[ -x "$UV" ] || UV="$(command -v uv)"
# The jobs stop before anything changes under them: a run in progress keeps the old release's modules and would
# import the new one's lazily mid-run. Timers too: they fire relative to a job's last start. What was running is
# noted first and started again at the end (an hourly collection would come back anyway; the nightly backup
# would wait for tomorrow).
systemctl stop 'drivecanary-*.timer' || true
INTERRUPTED=$(systemctl list-units --type=service --state=activating --plain --no-legend 'drivecanary-*.service' | awk '{print $1}')
for j in collect backup; do systemctl stop "drivecanary-$j.service" || true; done
# a oneshot stopped mid-run dies of SIGTERM and systemd calls it failed; only what WE stopped is cleared
for u in $INTERRUPTED; do systemctl reset-failed "$u" 2>/dev/null || true; done
sudo -u drivecanary -H env UV_PROJECT_ENVIRONMENT="$APP/.venv" "$UV" sync --frozen --no-dev --directory "$APP"
install -m 0644 "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer /etc/systemd/system/
install -m 0755 "$APP/deploy/drivecanary-cli" /usr/local/bin/drivecanary
install -o root -g root -m 0755 "$APP/deploy/update.sh" /usr/local/sbin/drivecanary-update  # next time's stage one
systemctl daemon-reload
sudo -u drivecanary -H env DRIVECANARY_CONFIG="$ETC/config.toml" "$APP/.venv/bin/drivecanary" config check
sudo -u drivecanary -H env DRIVECANARY_CONFIG="$ETC/config.toml" "$APP/.venv/bin/drivecanary" db migrate
date -Is > "$APP/.last-update"
systemctl restart drivecanary-web.service
# every timer enabled and running, by name: `systemctl start 'drivecanary-*.timer'` matches only loaded units,
# and after a `stop` the glob would start nothing
for t in "$APP"/deploy/systemd/*.timer; do systemctl enable --now "$(basename "$t")"; done
for u in $INTERRUPTED; do echo "starting $u again: the update interrupted it"; systemctl start --no-block "$u" || true; done
systemctl --no-pager --lines=5 status drivecanary-web.service

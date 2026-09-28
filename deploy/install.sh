#!/usr/bin/env bash
# First install on the Debian VM. Run as root from a checkout at /opt/drivecanary:
#   sudo deploy/install.sh
# Creates the two service users (the collector, which owns the database and the ssh key; the page, which may
# read the database and nothing else), the state dirs, the collector's key (printed: it goes into every host's
# install-host.sh run), installs the units, seeds config.toml, builds the venv, migrates, then enables the web
# service and the timers.
set -euo pipefail
APP=/opt/drivecanary
STATE=/var/lib/drivecanary
ETC=/etc/drivecanary
case "${1:-}" in help|--help|-h) echo "usage: sudo $0  -- first install on the VM: users, dirs, hub key, units, venv, CLI, migrate, enable timers (deploy/README.md)"; exit 0 ;; esac
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo deploy/install.sh)"; exit 1; }
[ -d "$APP/.git" ] || { echo "expected a checkout at $APP"; exit 1; }
getent group drivecanary >/dev/null || groupadd --system drivecanary
id drivecanary >/dev/null 2>&1 || useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin --gid drivecanary drivecanary
id drivecanary-web >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin --gid drivecanary drivecanary-web
# setgid on the state dir: the database and its -wal/-shm files stay in the shared group whoever makes them
install -d -o drivecanary -g drivecanary -m 2770 "$STATE"
install -d -o drivecanary -g drivecanary -m 0700 "$STATE/ssh"
install -d -o drivecanary -g drivecanary -m 0750 "$STATE/backups"
install -d -m 0750 -g drivecanary "$ETC"
[ -f "$ETC/config.toml" ] || { install -m 0640 -g drivecanary "$APP/deploy/config.example.toml" "$ETC/config.toml"; echo "seeded $ETC/config.toml -- check [web].bind and [collect]"; }
# the hub key: what every monitored host's install-host.sh binds to the gate
if [ ! -f "$STATE/ssh/id_ed25519" ]; then
  sudo -u drivecanary -H ssh-keygen -q -t ed25519 -N '' -C "drivecanary-hub@$(hostname)" -f "$STATE/ssh/id_ed25519"
  echo "hub key made: $STATE/ssh/id_ed25519.pub"
fi
# a deploy key, so `update.sh` can `git pull` as the service user over ssh: register the public key on the git
# server as a read-only deploy key of the repository, then accept the host key once (see the echo)
if [ ! -f "$STATE/.ssh/id_ed25519" ]; then
  install -d -m 0700 -o drivecanary -g drivecanary "$STATE/.ssh"
  sudo -u drivecanary -H ssh-keygen -q -t ed25519 -N '' -C "drivecanary@$(hostname) deploy key" -f "$STATE/.ssh/id_ed25519"
  echo "deploy key made -- register this public key on the git server (repository > deploy keys, read-only):"
  cat "$STATE/.ssh/id_ed25519.pub"
  echo "then accept the server's host key once:  sudo -u drivecanary -H git -C $APP fetch"
fi
chown -R drivecanary:drivecanary "$APP"
# uv: a per-user install (~/.local/bin) is invisible under sudo's PATH and to the service user, so find it
# wherever it is and put the one static binary in /usr/local/bin, where root, sudo and every user see it.
UV="$(command -v uv || true)"
for c in /usr/local/bin/uv /root/.local/bin/uv "/home/${SUDO_USER:-nobody}/.local/bin/uv"; do
  [ -n "$UV" ] || { [ -x "$c" ] && UV="$c"; }
done
[ -n "$UV" ] || { echo "uv is required: curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh"; exit 1; }
[ "$UV" = /usr/local/bin/uv ] || install -m 0755 "$UV" /usr/local/bin/uv
UV=/usr/local/bin/uv
sudo -u drivecanary -H env UV_PROJECT_ENVIRONMENT="$APP/.venv" "$UV" sync --frozen --no-dev --directory "$APP"
install -m 0644 "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer /etc/systemd/system/
chmod 0755 "$APP/deploy/backup.sh" "$APP/deploy/update.sh" "$APP/deploy/drivecanary-cli" "$APP/deploy/host/install-host.sh"
# `drivecanary ...` from any admin shell, as the service user with its config
install -m 0755 "$APP/deploy/drivecanary-cli" /usr/local/bin/drivecanary
# a root-owned copy of update.sh, the only thing sudoers needs to trust for `drivecanary update`
install -o root -g root -m 0755 "$APP/deploy/update.sh" /usr/local/sbin/drivecanary-update
systemctl daemon-reload
sudo -u drivecanary -H env DRIVECANARY_CONFIG="$ETC/config.toml" "$APP/.venv/bin/drivecanary" db migrate
chmod 0660 "$STATE"/*.db 2>/dev/null || true
date -Is > "$APP/.last-update"
systemctl enable --now drivecanary-web.service
for t in "$APP"/deploy/systemd/*.timer; do systemctl enable --now "$(basename "$t")"; done
systemctl list-timers 'drivecanary-*' --no-pager
echo
echo "installed. logs: journalctl -u 'drivecanary-*' -f; CLI: drivecanary --help (passwordless: deploy/sudoers.example)"
echo "next, per host, from your workstation:"
echo "  deploy/host/install-host.sh HOST --hub-ip $(hostname -I | awk '{print $1}') --hub-key '$(cat "$STATE/ssh/id_ed25519.pub")'"
echo "  drivecanary host add HOST --address HOST.domain --tz America/New_York --fingerprint SHA256:...   (as printed there)"
echo "  drivecanary collect --host HOST"

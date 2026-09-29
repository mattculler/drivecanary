#!/usr/bin/env bash
# Install, upgrade or repair drivecanary on ONE monitored host, from your workstation, over your own ssh to it
# (as root). Idempotent. Two ways a host can be monitored, and a host can be moved from one to the other by
# running this again the other way:
#
#   pull (default): the hub reaches the host over ssh. The hub's key is bound to the gate, from the hub's IP only.
#   push (--push):  the host reports to the hub. An agent runs from a timer and POSTs with the host's token;
#                   nothing on the host accepts a connection from the hub. For hosts that are only up on demand,
#                   and for the hypervisor the hub runs on.
#
# What it does on the host, either way:
#   1. checks smartctl is 7.x, installs sudo if absent
#   2. makes the `drivecanary` user (a real shell: a forced command runs through the login shell; password '*')
#   3. installs probe and gate root-owned in /usr/local/lib/drivecanary, the sudoers drop-in after visudo -c
# then, pull:
#   4. writes the user's authorized_keys: the hub's key, bound to the gate, from the hub's IP only
#   5. tests the gate and the probe as that user, and prints the host key fingerprint for the hub to pin
# or, push:
#   4. installs the agent, its config and token, and its timer; removes any authorized_keys left from a pull
#   5. runs the agent once and shows what the hub answered
# The hub can never do any of this: only your key can.
#
# OPNsense (FreeBSD) is push only. It keeps no account it did not make itself and lets only administrators in over
# ssh, so there the agent runs from root's cron (/usr/local/etc/cron.d/drivecanary, which OPNsense's own crontab
# names as the place for jobs of yours), with no user and no sudo. smartctl comes from the os-smart plugin.
#
# usage: deploy/host/install-host.sh HOST --hub-ip IP --hub-key FILE-OR-KEY [--target SSH-DEST]
#        deploy/host/install-host.sh HOST --push --hub-url URL --token TOKEN [--target SSH-DEST]
#   HOST        the name the host has (or will have) on the hub
#   --hub-ip    pull: the hub VM's IP, the only source the key is accepted from
#   --hub-key   pull: the hub's public key: /var/lib/drivecanary/ssh/id_ed25519.pub, or the key text
#   --hub-url   push: where the hub's ingest listens, e.g. http://10.100.100.50:8081
#   --token     push: the host's token, as `drivecanary host add HOST --transport push` printed it
#   --target    what to ssh to (default HOST; your ssh config decides the user)
set -euo pipefail
usage() {
  echo "usage: $0 HOST --hub-ip IP --hub-key FILE-OR-KEY [--target SSH-DEST]   (pull: the hub reaches the host over ssh)"
  echo "       $0 HOST --push --hub-url URL --token TOKEN [--target SSH-DEST]  (push: the host's agent reports to the hub)"
}
case "${1:-}" in ''|help|--help|-h) usage; exit 0 ;; esac
HOST=$1; shift
MODE=pull; HUB_IP=""; HUB_KEY=""; HUB_URL=""; TOKEN=""; TARGET=$HOST
while [ $# -gt 0 ]; do
  case "$1" in
    --push) MODE=push; shift ;;
    --hub-ip) HUB_IP=$2; shift 2 ;;
    --hub-key) HUB_KEY=$2; shift 2 ;;
    --hub-url) HUB_URL=${2%/}; shift 2 ;;
    --token) TOKEN=$2; shift 2 ;;
    --target) TARGET=$2; shift 2 ;;
    *) usage >&2; exit 64 ;;
  esac
done
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
FILES=(probe gate sudoers)
if [ "$MODE" = pull ]; then
  [ -n "$HUB_IP" ] && [ -n "$HUB_KEY" ] || { usage >&2; exit 64; }
  [ -f "$HUB_KEY" ] && HUB_KEY=$(cat "$HUB_KEY")
  case "$HUB_KEY" in ssh-ed25519\ *) ;; *) echo "--hub-key must be an ssh-ed25519 public key" >&2; exit 64 ;; esac
  printf 'restrict,from="%s",command="/usr/local/lib/drivecanary/gate" %s\n' "$HUB_IP" "$HUB_KEY" > "$WORK/authorized_keys"
else
  [ -n "$HUB_URL" ] && [ -n "$TOKEN" ] || { usage >&2; exit 64; }
  case "$HUB_URL" in http://*|https://*) ;; *) echo "--hub-url must start with http:// or https://" >&2; exit 64 ;; esac
  case "$TOKEN" in *[!A-Za-z0-9_-]*|'') echo "--token does not look like a drivecanary token" >&2; exit 64 ;; esac
  FILES+=(agent drivecanary-agent.service drivecanary-agent.timer)
  printf 'HUB_URL=%s\n' "$HUB_URL" > "$WORK/agent.conf"
  printf 'Authorization: Bearer %s\n' "$TOKEN" > "$WORK/agent.token"
fi
for f in "${FILES[@]}"; do
  [ -f "$HERE/$f" ] || { echo "missing $HERE/$f" >&2; exit 1; }
  cp "$HERE/$f" "$WORK/"
done
printf '%s\n' "$MODE" > "$WORK/mode"
cat > "$WORK/remote.sh" <<'REMOTE'
set -eu
T=$(cd "$(dirname "$0")" && pwd)
MODE=$(cat "$T/mode")
LIB=/usr/local/lib/drivecanary
if [ "$(uname -s)" = FreeBSD ]; then
  ETC=/usr/local/etc/drivecanary
  STATE=/var/db/drivecanary-agent
  CRON=/usr/local/etc/cron.d/drivecanary
  echo "== $(hostname): $(opnsense-version 2>/dev/null || echo FreeBSD) on FreeBSD $(freebsd-version) ($MODE)"
  if [ "$MODE" != push ]; then
    echo "this host is FreeBSD: only --push is supported here (see the top of install-host.sh)" >&2
    exit 64
  fi
  PATH=$PATH:/usr/local/sbin:/usr/local/bin
  if ! command -v smartctl >/dev/null 2>&1; then
    echo "smartctl missing: install the os-smart plugin (System > Firmware > Plugins), or pkg install smartmontools" >&2
    exit 1
  fi
  V=$(smartctl --version | head -1)
  echo "== $V"
  case "$V" in *" 7."*|*" 8."*) ;; *) echo "smartctl 7.0 or newer is needed for --json" >&2; exit 1 ;; esac
  for tool in flock curl gzip timeout; do
    command -v "$tool" >/dev/null 2>&1 || { echo "$tool is missing: pkg install $tool" >&2; exit 1; }
  done
  install -d -o 0 -g 0 -m 0755 "$LIB" "$ETC" /usr/local/etc/cron.d
  install -d -o 0 -g 0 -m 0700 "$STATE"
  install -o 0 -g 0 -m 0755 "$T/probe" "$T/gate" "$T/agent" "$LIB/"
  install -o 0 -g 0 -m 0644 "$T/agent.conf" "$ETC/agent.conf"
  install -o 0 -g 0 -m 0600 "$T/agent.token" "$ETC/agent.token"
  # hourly, at a minute of this host's own so that the hosts do not all report at once; and after a boot.
  # What the agent says goes to the system log (System > Log Files > General).
  MINUTE=$(( $(hostname | cksum | cut -d " " -f 1) % 60 ))
  {
    echo "# drivecanary: report this host's drive health to the hub (deploy/host/install-host.sh wrote this)"
    echo "SHELL=/bin/sh"
    echo "PATH=/etc:/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin:/usr/local/sbin"
    echo "$MINUTE	*	*	*	*	root	$LIB/agent 2>&1 | logger -t drivecanary-agent"
    echo "@reboot	root	sleep 120; $LIB/agent 2>&1 | logger -t drivecanary-agent"
  } > "$T/cron"
  install -o 0 -g 0 -m 0644 "$T/cron" "$CRON"
  echo "== $CRON: hourly at minute $MINUTE, and two minutes after a boot"
  echo "== self-test: probe (first frame)"
  "$LIB/probe" | head -1
  echo "== first report to $(sed -n 's/^HUB_URL=//p' "$ETC/agent.conf")"
  if "$LIB/agent"; then
    echo "== reported"
  else
    echo "the first report FAILED (above). cron will keep trying every hour; what was collected is spooled in $STATE." >&2
    exit 1
  fi
  exit 0
fi
HOME_DIR=/var/lib/drivecanary-probe
echo "== $(hostname): $(cat /etc/os-release 2>/dev/null | sed -n 's/^PRETTY_NAME="\(.*\)"/\1/p') ($MODE)"
if ! command -v smartctl >/dev/null 2>&1; then echo "smartctl missing: apt install smartmontools" >&2; exit 1; fi
V=$(smartctl --version | head -1)
echo "== $V"
case "$V" in *" 7."*|*" 8."*) ;; *) echo "smartctl 7.0 or newer is needed for --json" >&2; exit 1 ;; esac
command -v sudo >/dev/null 2>&1 || { echo "== installing sudo"; DEBIAN_FRONTEND=noninteractive apt-get install -y -q sudo; }
if ! id drivecanary >/dev/null 2>&1; then
  echo "== creating user drivecanary"
  adduser --system --group --shell /bin/sh --home "$HOME_DIR" --gecos "drivecanary probe" drivecanary >/dev/null
fi
# '*' = no password without the account lock that a leading '!' means to sshd
usermod -p '*' drivecanary
install -d -o root -g root -m 0755 "$LIB" /etc/drivecanary
install -o root -g root -m 0755 "$T/probe" "$LIB/probe"
install -o root -g root -m 0755 "$T/gate" "$LIB/gate"
visudo -c -q -f "$T/sudoers"
install -o root -g root -m 0440 "$T/sudoers" /etc/sudoers.d/drivecanary
echo "== self-test: gate"
su -s /bin/sh drivecanary -c 'SSH_ORIGINAL_COMMAND=drivecanary-ping /usr/local/lib/drivecanary/gate'
echo "== self-test: probe through sudo (first frame)"
su -s /bin/sh drivecanary -c 'sudo -n /usr/local/lib/drivecanary/probe' | head -1
echo "== attrlogs: $(ls /var/lib/smartmontools/attrlog.*.csv 2>/dev/null | wc -l) file(s)"
echo "== time zone: $(cat /etc/timezone 2>/dev/null || readlink -f /etc/localtime | sed 's|.*/zoneinfo/||')"
if [ "$MODE" = pull ]; then
  install -d -o drivecanary -g drivecanary -m 0700 "$HOME_DIR/.ssh"
  install -o drivecanary -g drivecanary -m 0600 "$T/authorized_keys" "$HOME_DIR/.ssh/authorized_keys"
  if sshd -T 2>/dev/null | grep -qiE '^allow(users|groups) '; then
    echo "NOTE: sshd restricts logins (AllowUsers/AllowGroups): add drivecanary, then reload sshd"
  fi
  if systemctl list-unit-files drivecanary-agent.timer >/dev/null 2>&1 && systemctl is-enabled -q drivecanary-agent.timer 2>/dev/null; then
    echo "== this host was a push host: stopping its agent"
    systemctl disable --now drivecanary-agent.timer
  fi
  echo "== host key fingerprint, for the hub to pin (drivecanary host add ... --fingerprint ...):"
  ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}'
else
  command -v curl >/dev/null 2>&1 || { echo "== installing curl"; DEBIAN_FRONTEND=noninteractive apt-get install -y -q curl; }
  install -o root -g root -m 0755 "$T/agent" "$LIB/agent"
  install -o root -g root -m 0644 "$T/agent.conf" /etc/drivecanary/agent.conf
  install -o drivecanary -g drivecanary -m 0600 "$T/agent.token" /etc/drivecanary/agent.token
  install -o root -g root -m 0644 "$T/drivecanary-agent.service" "$T/drivecanary-agent.timer" /etc/systemd/system/
  # a push host holds no key of the hub's: what a pull left behind goes
  if [ -f "$HOME_DIR/.ssh/authorized_keys" ]; then
    echo "== this host was a pull host: removing the hub's key"
    rm -f "$HOME_DIR/.ssh/authorized_keys"
  fi
  systemctl daemon-reload
  systemctl enable -q --now drivecanary-agent.timer
  echo "== first report to $(sed -n 's/^HUB_URL=//p' /etc/drivecanary/agent.conf)"
  if systemctl start drivecanary-agent.service; then
    journalctl -u drivecanary-agent.service -n 3 --no-pager -o cat
  else
    journalctl -u drivecanary-agent.service -n 8 --no-pager -o cat
    echo "the first report FAILED (above). The timer is enabled and will keep trying; what was collected is spooled." >&2
    exit 1
  fi
  systemctl list-timers drivecanary-agent.timer --no-pager | sed -n '1,2p'
fi
REMOTE
echo "installing on $HOST via ssh $TARGET ($MODE)"
# through `sh -c`: the login shell on the other end need not be a Bourne shell (OPNsense's root has csh)
tar -C "$WORK" -cf - . | ssh "$TARGET" "sh -c 'T=\$(mktemp -d /tmp/drivecanary-install.XXXXXX) && tar -C \$T -xf - && sh \$T/remote.sh; rc=\$?; rm -rf \$T; exit \$rc'"
if [ "$MODE" = pull ]; then
  echo "done. On the hub: drivecanary host add $HOST --address <hostname> --fingerprint SHA256:<above>; then drivecanary collect --host $HOST"
else
  echo "done. $HOST reports hourly, and two minutes after it boots. On the hub: drivecanary status"
fi

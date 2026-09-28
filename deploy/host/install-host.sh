#!/usr/bin/env bash
# Install, upgrade or repair the drivecanary probe on ONE monitored host, from your workstation, over your own
# ssh to it (as root). Idempotent. What it does there:
#   1. checks smartctl is 7.x, installs sudo if absent
#   2. makes the `drivecanary` user (a real shell: a forced command runs through the login shell; password '*')
#   3. installs probe and gate root-owned in /usr/local/lib/drivecanary, the sudoers drop-in after visudo -c
#   4. writes the user's authorized_keys: the hub's key, bound to the gate, from the hub's IP only
#   5. tests the gate and the probe as that user, and prints the host key fingerprint for the hub to pin
# The hub's key can never do any of this: only your key can.
#
# usage: deploy/host/install-host.sh HOST --hub-ip IP --hub-key FILE-OR-KEY [--target SSH-DEST]
#   HOST        the name you will give the host on the hub (used for messages only)
#   --hub-ip    the hub VM's static IP: the only source the key is accepted from
#   --hub-key   the hub's public key: /var/lib/drivecanary/ssh/id_ed25519.pub copied here, or the key text
#   --target    what to ssh to (default HOST; your ssh config decides the user)
set -euo pipefail
usage() {
  echo "usage: $0 HOST --hub-ip IP --hub-key FILE-OR-KEY [--target SSH-DEST]  -- install/upgrade the probe, gate, sudoers and authorized_keys on one host over your own root ssh"
}
case "${1:-}" in ''|help|--help|-h) usage; exit 0 ;; esac
HOST=$1; shift
HUB_IP=""; HUB_KEY=""; TARGET=$HOST
while [ $# -gt 0 ]; do
  case "$1" in
    --hub-ip) HUB_IP=$2; shift 2 ;;
    --hub-key) HUB_KEY=$2; shift 2 ;;
    --target) TARGET=$2; shift 2 ;;
    *) usage >&2; exit 64 ;;
  esac
done
[ -n "$HUB_IP" ] && [ -n "$HUB_KEY" ] || { usage >&2; exit 64; }
[ -f "$HUB_KEY" ] && HUB_KEY=$(cat "$HUB_KEY")
case "$HUB_KEY" in ssh-ed25519\ *) ;; *) echo "--hub-key must be an ssh-ed25519 public key" >&2; exit 64 ;; esac
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
for f in probe gate sudoers; do [ -f "$HERE/$f" ] || { echo "missing $HERE/$f" >&2; exit 1; }; done

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp "$HERE/probe" "$HERE/gate" "$HERE/sudoers" "$WORK/"
printf 'restrict,from="%s",command="/usr/local/lib/drivecanary/gate" %s\n' "$HUB_IP" "$HUB_KEY" > "$WORK/authorized_keys"
cat > "$WORK/remote.sh" <<'REMOTE'
set -eu
T=$(cd "$(dirname "$0")" && pwd)
LIB=/usr/local/lib/drivecanary
HOME_DIR=/var/lib/drivecanary-probe
echo "== $(hostname): $(cat /etc/os-release 2>/dev/null | sed -n 's/^PRETTY_NAME="\(.*\)"/\1/p')"
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
install -d -o drivecanary -g drivecanary -m 0700 "$HOME_DIR/.ssh"
install -o drivecanary -g drivecanary -m 0600 "$T/authorized_keys" "$HOME_DIR/.ssh/authorized_keys"
if sshd -T 2>/dev/null | grep -qiE '^allow(users|groups) '; then
  echo "NOTE: sshd restricts logins (AllowUsers/AllowGroups): add drivecanary, then reload sshd"
fi
echo "== self-test: gate"
su -s /bin/sh drivecanary -c 'SSH_ORIGINAL_COMMAND=drivecanary-ping /usr/local/lib/drivecanary/gate'
echo "== self-test: probe through sudo (first frame)"
su -s /bin/sh drivecanary -c 'sudo -n /usr/local/lib/drivecanary/probe' | head -1
echo "== attrlogs: $(ls /var/lib/smartmontools/attrlog.*.csv 2>/dev/null | wc -l) file(s)"
echo "== time zone: $(cat /etc/timezone 2>/dev/null || readlink -f /etc/localtime | sed 's|.*/zoneinfo/||')"
echo "== host key (pin this on the hub: drivecanary host add ... --fingerprint SHA256:...):"
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
REMOTE
echo "installing on $HOST via ssh $TARGET"
tar -C "$WORK" -cf - probe gate sudoers authorized_keys remote.sh | ssh "$TARGET" 'set -e; T=$(mktemp -d); tar -C "$T" -xf -; sh "$T/remote.sh"; rm -rf "$T"'
echo "done. On the hub: drivecanary host add $HOST --address <hostname> --tz <zone> --fingerprint SHA256:<above>; then drivecanary collect --host $HOST"

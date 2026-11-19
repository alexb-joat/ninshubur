#!/usr/bin/env sh
# Install (or remove with --remove) a hardened systemd *user* timer that runs one explorer cycle,
# `ninshubur explore run`, every hour. --print shows both units and installs nothing.
# Usage: deploy/linux/install-explorer.sh [--print | --remove]   (run from the repo root with the venv created)
#
# The explorer is separate from the main daemon (deploy/linux/install.sh): its own service, its own limits, and it
# can write only its own folder ($NINSHUBUR_HOME/explorer). Two ways to stop it:
#   ninshubur explore stop --reason "..."      the STOP file; the unit won't even start while it exists
#   systemctl --user stop ninshubur-explorer.timer ninshubur-explorer.service
set -eu
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
HOME_DIR="${NINSHUBUR_HOME:-$HOME/.ninshubur}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$ROOT/.venv/bin/python"
LIMIT="${NINSHUBUR_EXPLORE_LIMIT:-10}"

service() {
  cat <<EOF
[Unit]
Description=Ninshubur explorer: one cycle (probe, fetch, parse in bwrap, trial ladder)
After=network-online.target
# The kill switch: no cycle starts while the STOP file exists.
ConditionPathExists=!$HOME_DIR/explorer/STOP

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=NINSHUBUR_HOME=$HOME_DIR
ExecStart=$PY -m ninshubur explore run --limit $LIMIT
TimeoutStartSec=30min
Nice=10
# Writes only its own folder; reads the code and the main config.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$HOME_DIR/explorer
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectKernelLogs=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
ProcSubset=pid
PrivateDevices=yes
CapabilityBoundingSet=
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
UMask=0077
# The parser sandbox (bwrap) needs user, mount, network and pid namespaces and netlink to bring up its loopback,
# so RestrictNamespaces= is left off, AF_NETLINK is allowed, and the syscall filter adds @mount.
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
SystemCallFilter=@system-service @mount
# Resource ceilings for the whole cycle, parser included (one parse is capped at 1 GB inside the sandbox).
MemoryMax=1536M
CPUQuota=50%
TasksMax=64
EOF
}

timer() {
  cat <<EOF
[Unit]
Description=Ninshubur explorer: one cycle an hour

[Timer]
OnCalendar=hourly
RandomizedDelaySec=10min
Persistent=false

[Install]
WantedBy=timers.target
EOF
}

case "${1:-}" in
  --print)
    echo "# $UNIT_DIR/ninshubur-explorer.service"; service
    echo; echo "# $UNIT_DIR/ninshubur-explorer.timer"; timer
    exit 0 ;;
  --remove)
    systemctl --user disable --now ninshubur-explorer.timer 2>/dev/null || true
    systemctl --user stop ninshubur-explorer.service 2>/dev/null || true
    rm -f "$UNIT_DIR/ninshubur-explorer.service" "$UNIT_DIR/ninshubur-explorer.timer"
    systemctl --user daemon-reload
    echo "removed the explorer's service and timer from $UNIT_DIR"
    exit 0 ;;
  "") ;;
  *) echo "usage: $0 [--print | --remove]" >&2; exit 2 ;;
esac

[ -x "$PY" ] || { echo "no venv at $ROOT/.venv; create it and pip install -e . first" >&2; exit 1; }
command -v bwrap >/dev/null || { echo "bwrap not found: install bubblewrap first (the explorer never parses without it)" >&2; exit 1; }
"$PY" -m ninshubur explore check >/dev/null || { echo "\`ninshubur explore check\` failed: the sandbox isn't fenced here; not installing" >&2; exit 1; }
mkdir -p "$UNIT_DIR" "$HOME_DIR/explorer" "$HOME_DIR/logs" "$HOME_DIR/feeds"  # the unit can write only explorer/
chmod 700 "$HOME_DIR" "$HOME_DIR/explorer"
service > "$UNIT_DIR/ninshubur-explorer.service"
timer > "$UNIT_DIR/ninshubur-explorer.timer"
systemctl --user daemon-reload
systemctl --user enable --now ninshubur-explorer.timer
echo "installed the explorer timer (hourly); next run: systemctl --user list-timers ninshubur-explorer.timer"
echo "logs: journalctl --user -u ninshubur-explorer; stop: ninshubur explore stop --reason \"...\""

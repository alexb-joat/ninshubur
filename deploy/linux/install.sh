#!/usr/bin/env sh
# Install (or remove with --remove) a hardened systemd *user* unit that runs `ninshubur run --daemon`.
# Usage: deploy/linux/install.sh [--remove]   (run from the repo root with the venv created)
set -eu
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/ninshubur.service"
HOME_DIR="${NINSHUBUR_HOME:-$HOME/.ninshubur}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$ROOT/.venv/bin/python"

if [ "${1:-}" = "--remove" ]; then
  systemctl --user disable --now ninshubur.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo "removed $UNIT"
  exit 0
fi

[ -x "$PY" ] || { echo "no venv at $ROOT/.venv; create it and pip install -e . first" >&2; exit 1; }
mkdir -p "$UNIT_DIR" "$HOME_DIR"
chmod 700 "$HOME_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=Ninshubur policy-first crawler
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=NINSHUBUR_HOME=$HOME_DIR
ExecStart=$PY -m ninshubur run --daemon
Restart=on-failure
RestartSec=30
# Hardening (F5 in the plan): the process can write only its own data directory.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$HOME_DIR
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
MemoryMax=512M
CapabilityBoundingSet=
UMask=0077

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now ninshubur.service
echo "installed $UNIT; logs: journalctl --user -u ninshubur -f"

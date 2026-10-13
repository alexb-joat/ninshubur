#!/usr/bin/env sh
# Install (or remove with --remove) a launchd user agent that runs `ninshubur run --daemon`.
set -eu
PLIST="$HOME/Library/LaunchAgents/ai.leadget.ninshubur.plist"
HOME_DIR="${NINSHUBUR_HOME:-$HOME/.ninshubur}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$ROOT/.venv/bin/python"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $PLIST"
  exit 0
fi

[ -x "$PY" ] || { echo "no venv at $ROOT/.venv; create it and pip install -e . first" >&2; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$HOME_DIR"
chmod 700 "$HOME_DIR"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.leadget.ninshubur</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>-m</string><string>ninshubur</string><string>run</string><string>--daemon</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict><key>NINSHUBUR_HOME</key><string>$HOME_DIR</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$HOME_DIR/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/logs/launchd.err.log</string>
  <key>Umask</key><integer>63</integer>
</dict></plist>
EOF
mkdir -p "$HOME_DIR/logs"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $PLIST"

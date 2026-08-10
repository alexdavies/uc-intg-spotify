#!/bin/sh
# One-shot installer: create the venv, install deps, and register a launchd
# service that auto-starts the B&O integration and restarts it if it crashes.
# Run this ON THE MAC MINI, from anywhere:  sh bang-olufsen/deploy/install.sh
set -e

HERE="$(cd "$(dirname "$0")/.." && pwd)"   # the bang-olufsen directory
LABEL="com.beo.bang-olufsen"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/beo-bang-olufsen.log"

PY="$(command -v python3.11 || command -v python3 || true)"
[ -n "$PY" ] || { echo "Python 3 not found. Install it (e.g. 'brew install python@3.11')."; exit 1; }
echo "Using Python: $PY"

# 1. venv + dependencies (includes pychromecast, mozart-api, ucapi, ...)
[ -d "$HERE/.venv" ] || "$PY" -m venv "$HERE/.venv"
"$HERE/.venv/bin/pip" install -q --upgrade pip
"$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"

chmod +x "$HERE/deploy/run.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

# 2. launchd service (LaunchAgent: starts on login; see README for boot-time).
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$HERE/deploy/run.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

# 3. (re)load it
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo
echo "Installed and started: $LABEL"
echo "  Logs:    $LOG"
echo "  Stop:    launchctl unload $PLIST"
echo "  Start:   launchctl load $PLIST"
echo
echo "If you haven't already, copy your existing config.json (with Spotify tokens"
echo "+ speakers) to:  $HERE/config.json   — otherwise run setup on the Remote."

#!/bin/sh
# Run Quillwork in the background on macOS: start at login, restart if it crashes.
# Usage: scripts/autostart.sh install | uninstall | status | wake
set -e
LABEL=com.quillwork.app
DIR=$(cd "$(dirname "$0")/.." && pwd)
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "$1" in
install)
  [ -x "$DIR/.venv/bin/python" ] || { echo "Missing $DIR/.venv. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
  if lsof -tiTCP:8787 -sTCP:LISTEN >/dev/null 2>&1 && ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "Port 8787 is in use by another Quillwork. Stop it first (Ctrl+C in its terminal)."; exit 1
  fi
  mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/.venv/bin/python</string><string>$DIR/app.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$DIR/logs/quillwork.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/quillwork.log</string>
</dict></plist>
EOF
  launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "Installed. Quillwork now starts at login: http://localhost:8787 (logs: $DIR/logs/quillwork.log)"
  ;;
uninstall)
  launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed. Quillwork no longer starts at login."
  ;;
status)
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl print "$DOMAIN/$LABEL" | grep -E "^\s*(state|pid|last exit code) =" | sed 's/^[[:space:]]*//'
  else
    echo "Not installed."
  fi
  ;;
wake)
  # pmset holds one repeating wake schedule, so wake 5 minutes before the earliest posting time.
  "$DIR/.venv/bin/python" - "$DIR/settings.json" <<'PY'
import json, sys
from datetime import datetime, timedelta
s = json.load(open(sys.argv[1]))["schedule"]
code = dict(Mon="M", Tue="T", Wed="W", Thu="R", Fri="F", Sat="S", Sun="U")
days = "".join(code[d] for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] if d in s["days"])
t = (datetime.strptime(min(s["times"]), "%H:%M") - timedelta(minutes=5)).strftime("%H:%M:00")
print(f"Run this once (it asks for your Mac password):\n  sudo pmset repeat wakeorpoweron {days} {t}")
if len(s["times"]) > 1:
    print("Note: macOS allows one repeating wake time, so only the earliest slot is covered.")
print("Check it with: pmset -g sched    Remove it with: sudo pmset repeat cancel")
PY
  ;;
*)
  echo "Usage: $0 install | uninstall | status | wake"; exit 1
  ;;
esac

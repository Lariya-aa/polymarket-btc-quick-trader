#!/bin/zsh
set -euo pipefail

APP_HOME="$(cd "$(dirname "$0")" && pwd)"
PLIST_ID="com.example.polymm"
SRC="$APP_HOME/launchd/$PLIST_ID.plist"
DST="$HOME/Library/LaunchAgents/$PLIST_ID.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed "s#__APP_HOME__#$APP_HOME#g" "$SRC" > "$DST"
launchctl bootout "gui/$(id -u)" "$DST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$DST"
launchctl enable "gui/$(id -u)/$PLIST_ID"

echo "Installed and started $PLIST_ID"
echo "Logs:"
echo "  $APP_HOME/poly_mm_launchd.out.log"
echo "  $APP_HOME/poly_mm_launchd.err.log"

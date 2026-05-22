#!/bin/zsh
set -euo pipefail

PLIST_ID="com.example.polymm"
DST="$HOME/Library/LaunchAgents/$PLIST_ID.plist"

launchctl bootout "gui/$(id -u)" "$DST" >/dev/null 2>&1 || true
rm -f "$DST"

echo "Stopped and removed $PLIST_ID"

#!/bin/zsh
set -euo pipefail

APP_HOME="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$APP_HOME/poly_mm_watchdog.log"

cd "$APP_HOME"

while true; do
  if [[ -f "$HOME/.poly_mm_env" ]]; then
    set -a
    source "$HOME/.poly_mm_env"
    set +a
  fi

  echo "$(date '+%Y-%m-%d %H:%M:%S') starting guarded GUI" >> "$LOG_FILE"
  python3 "$APP_HOME/poly_mm_pro_max.py" >> "$LOG_FILE" 2>&1
  status=$?
  echo "$(date '+%Y-%m-%d %H:%M:%S') GUI exited with status $status" >> "$LOG_FILE"

  if [[ "$status" -eq 0 ]]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') normal exit, watchdog stops" >> "$LOG_FILE"
    exit 0
  fi

  sleep 5
done

#!/bin/zsh
set -euo pipefail

APP_HOME="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$APP_HOME/poly_mm_launcher.log"

cd "$APP_HOME"

if [[ -f "$HOME/.poly_mm_env" ]]; then
  set -a
  source "$HOME/.poly_mm_env"
  set +a
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') launching PolyMarketMaker" >> "$LOG_FILE"
exec python3 "$APP_HOME/poly_mm_pro_max.py"

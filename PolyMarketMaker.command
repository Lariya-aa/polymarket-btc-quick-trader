#!/bin/zsh
APP_HOME="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_HOME" || exit 1
set -a
source ~/.poly_mm_env 2>/dev/null || true
set +a
python3 "$APP_HOME/poly_mm_pro_max.py"

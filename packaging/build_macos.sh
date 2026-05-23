#!/bin/zsh
# Build a macOS .app + .dmg for PolyMarketTrader.
#
# Usage:
#   ./packaging/build_macos.sh                  # build into dist/
#   CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#     ./packaging/build_macos.sh                 # also sign the .app
#
# Outputs:
#   dist/PolyMarketTrader.app
#   dist/PolyMarketTrader.dmg
#
# Prereqs:
#   - macOS 11+ (Big Sur)
#   - Python 3.10+ matching the version the user runs source-mode with
#   - .venv with -r requirements-dev.txt (gives us pyinstaller)
#   - hdiutil (ships with macOS)
#
# This script is intentionally cautious:
#   - aborts on any error (`set -euo pipefail`)
#   - refuses to clobber a non-empty dist/ silently — pass FORCE=1 to
#     override
#   - skips codesign if CODESIGN_IDENTITY is unset, but logs a clear
#     warning so the user knows the resulting .app will trip Gatekeeper

set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
APP_NAME="PolyMarketTrader"
DIST_DIR="$REPO_ROOT/dist"
SPEC="packaging/poly_mm.spec"

# ── pre-flight ───────────────────────────────────────────────────────────
if [[ "$(uname)" != "Darwin" ]]; then
  echo "❌ This script must run on macOS (saw $(uname))." >&2
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  echo "❌ Expected a .venv/ in repo root. Create it first:" >&2
  echo "     python3 -m venv .venv" >&2
  echo "     .venv/bin/pip install -r requirements-dev.txt pyinstaller" >&2
  exit 1
fi

if [[ -d "$DIST_DIR" && -n "$(ls -A "$DIST_DIR" 2>/dev/null)" && "${FORCE:-0}" != "1" ]]; then
  echo "❌ dist/ is not empty. Re-run with FORCE=1 to wipe it." >&2
  exit 1
fi

# ── build ────────────────────────────────────────────────────────────────
echo "==> Cleaning build artifacts"
rm -rf build "$DIST_DIR"

echo "==> Running PyInstaller"
.venv/bin/pyinstaller "$SPEC" --clean --noconfirm

APP_PATH="$DIST_DIR/$APP_NAME.app"
if [[ ! -d "$APP_PATH" ]]; then
  echo "❌ Expected $APP_PATH after PyInstaller, not found." >&2
  exit 1
fi

# ── codesign (optional) ──────────────────────────────────────────────────
if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  echo "==> Code-signing with: $CODESIGN_IDENTITY"
  codesign --deep --force --verify --verbose \
    --sign "$CODESIGN_IDENTITY" \
    --options runtime \
    "$APP_PATH"
  codesign --verify --verbose=2 "$APP_PATH"
else
  cat >&2 <<'EOF'

⚠  CODESIGN_IDENTITY not set — the .app is unsigned.
   First-time users will see Gatekeeper warnings:
     "PolyMarketTrader.app can't be opened because it is from an unidentified developer."
   They can bypass with: right-click > Open (once), or
     xattr -dr com.apple.quarantine PolyMarketTrader.app
   For wide distribution: get a Developer ID cert + notarize.
   See PACKAGING.md.

EOF
fi

# ── dmg ──────────────────────────────────────────────────────────────────
DMG_PATH="$DIST_DIR/$APP_NAME.dmg"
echo "==> Building $DMG_PATH"
hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$APP_PATH" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

# ── summary ──────────────────────────────────────────────────────────────
echo ""
echo "✅ Built:"
echo "    $APP_PATH"
ls -lh "$APP_PATH" | awk '{print "      ("$5")"}'
echo "    $DMG_PATH"
ls -lh "$DMG_PATH" | awk '{print "      ("$5")"}'
echo ""
echo "Smoke test the .app before distributing:"
echo "    open $APP_PATH"

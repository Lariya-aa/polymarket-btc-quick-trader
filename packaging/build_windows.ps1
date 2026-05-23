# Build a Windows .exe (in a folder bundle) for PolyMarketTrader.
#
# Usage (PowerShell, from repo root):
#   .\packaging\build_windows.ps1
#
# Outputs:
#   dist\PolyMarketTrader\PolyMarketTrader.exe   (entry point)
#   dist\PolyMarketTrader\                       (folder with all bundled deps)
#
# Prereqs:
#   - Windows 10/11
#   - Python 3.10+ on PATH
#   - .venv with: python -m pip install -r requirements-dev.txt pyinstaller
#
# To produce a single-file installer instead, wrap the dist folder with
# Inno Setup or NSIS. See PACKAGING.md for one example .iss file.

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$APP_NAME = "PolyMarketTrader"
$DIST_DIR = "dist"
$SPEC = "packaging\poly_mm.spec"

# Pre-flight: venv?
if (-not (Test-Path ".venv")) {
    Write-Error @"
Expected a .venv\ in repo root. Create it first:
  python -m venv .venv
  .venv\Scripts\pip install -r requirements-dev.txt pyinstaller
"@
    exit 1
}

# Pre-flight: dist dir not already populated unless FORCE.
if ((Test-Path $DIST_DIR) -and ((Get-ChildItem $DIST_DIR -Force | Measure-Object).Count -gt 0)) {
    if ($env:FORCE -ne "1") {
        Write-Error "dist\ is not empty. Re-run with `$env:FORCE='1' to wipe it."
        exit 1
    }
}

Write-Host "==> Cleaning build artifacts"
Remove-Item -Recurse -Force build, $DIST_DIR -ErrorAction SilentlyContinue

Write-Host "==> Running PyInstaller"
& .venv\Scripts\pyinstaller.exe $SPEC --clean --noconfirm

$EXE_PATH = Join-Path $DIST_DIR "$APP_NAME\$APP_NAME.exe"
if (-not (Test-Path $EXE_PATH)) {
    Write-Error "Expected $EXE_PATH after PyInstaller, not found."
    exit 1
}

Write-Host ""
Write-Host "Built:" -ForegroundColor Green
Write-Host "    $EXE_PATH"
$size = (Get-Item $EXE_PATH).Length
Write-Host ("      ({0:N1} MB)" -f ($size / 1MB))
Write-Host ""
Write-Host "Smoke test before distributing:"
Write-Host "    & '$EXE_PATH'"
Write-Host ""
Write-Host "Note: unsigned binaries trigger SmartScreen on first launch."
Write-Host "See PACKAGING.md for code-signing options."

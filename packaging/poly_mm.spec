# PyInstaller spec for PolyMarketTrader.
#
# Build:
#   pyinstaller packaging/poly_mm.spec --clean --noconfirm
#
# Outputs (depending on host OS):
#   macOS:   dist/PolyMarketTrader.app
#   Windows: dist/PolyMarketTrader/PolyMarketTrader.exe (onedir mode)
#
# Why onedir not onefile:
#   --onefile unpacks the bundle to a temp dir every launch (~2s extra
#   startup on macOS, ~5s on Windows). For a trading tool where the
#   user wants the GUI up fast, onedir wins. The .app and .dmg wrap the
#   onedir tree on macOS; Windows users get a folder they can move
#   anywhere.
#
# Why no --onefile fallback:
#   Trying to keep two build variants doubles QA surface and the .dmg /
#   .exe artifacts only need one layout each.

import sys

block_cipher = None
APP_NAME = "PolyMarketTrader"
SCRIPT = "poly_mm_pro_max.py"

# py_clob_client_v2 has eth-account and web3 transitive deps whose
# submodule layout sometimes hides from PyInstaller's static analysis.
# Force-include the modules we observed py_clob_client_v2 importing at
# runtime so a fresh build doesn't ship a broken bundle.
hidden = [
    "py_clob_client_v2",
    "py_clob_client_v2.client",
    "py_clob_client_v2.clob_types",
    "py_clob_client_v2.order_builder",
    "py_clob_client_v2.signer",
    "eth_account",
    "eth_account.messages",
    "eth_utils",
    "eth_keys",
]

a = Analysis(
    [SCRIPT],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Trim things we know we don't use to keep bundle small.
        "matplotlib",
        "numpy.testing",
        "pytest",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX can trip antivirus and trims very little here.
    console=False,  # no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

# macOS-only: wrap the onedir COLLECT into a .app bundle.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=None,  # TODO: add packaging/icon.icns
        bundle_identifier="com.polymarket.quicktrader",
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": "Polymarket Quick Trader",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "NSHighResolutionCapable": True,
            # Tkinter apps don't need camera/mic/etc — keep usage
            # description keys absent so Gatekeeper doesn't ask.
            "LSMinimumSystemVersion": "11.0",
        },
    )

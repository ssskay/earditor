# -*- mode: python ; coding: utf-8 -*-
"""
setup_win.spec — PyInstaller build config for Earditor on Windows.

    pip install -r requirements.txt -r packaging/requirements-app.txt pyinstaller
    pyinstaller packaging/setup_win.spec --noconfirm

Produces dist/Earditor/Earditor.exe (one-dir, not one-file: one-file unpacks to a
temp dir on every launch, which is slow and confuses the vendored-fpcalc lookup).

py2app is macOS-only, which is why Windows needs its own spec — see setup_app.py
for the mac build. The two must stay behaviorally identical, --demo included.

Notes:
- pywebview uses WebView2 on Windows (preinstalled on Win 11; Win 10 users may
  need the Evergreen runtime — the README links it).
- librosa/scipy/numba are deliberately excluded, matching setup_app.py: align.py
  lazy-imports librosa and degrades to "align n/a".
- The exe is unsigned; a Windows code-signing cert is out of scope for v1, so
  SmartScreen shows "More info → Run anyway" on first launch.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Read-only resources. Flask needs review.html on disk at runtime; review.py
# resolves it via config.resource_root(), which reads sys._MEIPASS under PyInstaller.
datas = [
    (str(ROOT / "templates" / "review.html"), "templates"),
    (str(ROOT / "demo" / "fixtures.json"), "demo"),
    (str(ROOT / "config.example.json"), "."),
]

# pykakasi ships its dictionary as package data — without this, romaji (S4) breaks
# only at runtime, and only on Japanese text, which is a miserable way to find out.
datas += collect_data_files("pykakasi")

# Vendored fpcalc.exe, so AcoustID works with nothing installed. The build workflow
# downloads it from Chromaprint's official release; if it's absent we still build,
# and AcoustID just degrades to "no key/binary" as it does from source.
_fpcalc = ROOT / "packaging" / "vendor" / "fpcalc.exe"
if _fpcalc.exists():
    datas.append((str(_fpcalc), "."))
else:
    print("WARNING: packaging/vendor/fpcalc.exe missing — building without AcoustID "
          "fingerprinting. See PACKAGING.md.")

# shazamio's async stack resolves plugins dynamically; PyInstaller's static analysis
# misses those, and the failure only shows up at runtime on the first scan.
hiddenimports = (
    collect_submodules("shazamio")
    + collect_submodules("pykakasi")
    + ["acoustid", "musicbrainzngs", "chardet", "charset_normalizer",
       "webview.platforms.edgechromium"]
)

a = Analysis(
    [str(ROOT / "packaging" / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "pytest", "_pytest", "tkinter",
        "librosa", "scipy", "numba", "llvmlite", "soundfile", "audioread",
        "sklearn", "matplotlib",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Earditor",
    debug=False,
    strip=False,
    upx=False,          # UPX-packed exes trip antivirus heuristics; not worth it
    console=False,      # GUI app: no console window behind the pywebview shell
    icon=str(ROOT / "packaging" / "assets" / "Earditor.ico")
    if (ROOT / "packaging" / "assets" / "Earditor.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Earditor",
)

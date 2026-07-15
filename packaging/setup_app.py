#!/usr/bin/env python3
"""
setup_app.py — py2app build config for a native, notarizable Earditor.app.

    pip install -r packaging/requirements-app.txt
    python3 packaging/setup_app.py py2app

Then code-sign with your Developer ID and notarize for DIRECT distribution — see
PACKAGING.md. (The Mac App Store is a poor fit: its sandbox fights the AppleScript
control of Music.app and the arbitrary library-file access Earditor needs.)

Release builds still need Developer ID signing and notarization; see PACKAGING.md.
"""

import sys
from pathlib import Path

from setuptools import setup

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP = ["packaging/app.py"]

# Flask needs the template on disk at runtime; bundle the templates/ folder as a
# resource. review.py resolves render_template("review.html") relative to itself.
DATA_FILES = [
    ("templates", ["templates/review.html"]),
    ("demo", ["demo/fixtures.json"]),
    ("", ["config.example.json"]),
]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "packaging/assets/Earditor.icns",
    "excludes": ["pytest", "_pytest", "setuptools.tests", "numpy.tests", "tkinter"],
    "includes": [
        "review", "verify", "utils", "covers", "tagger", "db", "config",
        "itunes_bridge", "scan", "refresh_artwork",
    ],
    "packages": [
        "flask", "jinja2", "mutagen", "rapidfuzz", "pykakasi",
        "shazamio", "acoustid", "musicbrainzngs", "requests",
        "charset_normalizer", "chardet", "webview", "sources",
    ],
    "plist": {
        "CFBundleName": "Earditor",
        "CFBundleDisplayName": "Earditor",
        # Change to your own reverse-domain identifier before signing.
        "CFBundleIdentifier": "me.sarakay.earditor",
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "Copyright © 2026 Sara Kay. MIT licensed.",
        # Required so macOS shows a clear prompt when Earditor automates Music.app.
        "NSAppleEventsUsageDescription":
            "Earditor adds tagged tracks to your Music library and refreshes their artwork.",
    },
}

setup(
    name="Earditor",
    version="0.1.0",
    description="Evidence-based music metadata review for macOS",
    author="Sara Kay",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)

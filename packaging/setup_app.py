#!/usr/bin/env python3
"""
setup_app.py — py2app build config for a native, notarizable Earditor.app.

    pip install -r packaging/requirements-app.txt
    python3 packaging/setup_app.py py2app

Then code-sign with your Developer ID and notarize for DIRECT distribution — see
PACKAGING.md. (The Mac App Store is a poor fit: its sandbox fights the AppleScript
control of Music.app and the arbitrary library-file access Earditor needs.)

This is a starting point, not a turnkey build — expect to iterate on the includes/
packages lists and on template bundling (see PACKAGING.md § Known wrinkles).
"""

from setuptools import setup

APP = ["packaging/app.py"]

# Flask needs the template on disk at runtime; bundle the templates/ folder as a
# resource. review.py resolves render_template("review.html") relative to itself.
DATA_FILES = [
    ("templates", ["templates/review.html"]),
    ("", ["config.example.json"]),
]

OPTIONS = {
    "argv_emulation": False,
    "includes": [
        "review", "verify", "utils", "covers", "tagger", "db", "config",
        "itunes_bridge", "scan", "refresh_artwork",
    ],
    "packages": [
        "flask", "jinja2", "mutagen", "rapidfuzz", "pykakasi",
        "shazamio", "acoustid", "musicbrainzngs", "requests", "webview",
    ],
    "plist": {
        "CFBundleName": "Earditor",
        "CFBundleDisplayName": "Earditor",
        # Change to your own reverse-domain identifier before signing.
        "CFBundleIdentifier": "me.sarakay.earditor",
        "LSMinimumSystemVersion": "12.0",
        # Required so macOS shows a clear prompt when Earditor automates Music.app.
        "NSAppleEventsUsageDescription":
            "Earditor adds tagged tracks to your Music library and refreshes their artwork.",
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)

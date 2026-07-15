#!/usr/bin/env python3
"""
config.py — configuration loader for Earditor.

Loads config.json (music_path, thresholds, feature flags) and resolves the
AcoustID API key from the environment or the macOS Keychain.

IMPORTANT: the AcoustID key is NEVER written to config.json or any file on disk.
It is read at runtime from:
  1. $ACOUSTID_API_KEY, or
  2. macOS Keychain: `security find-generic-password -s acoustid -w`
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("earditor.config")

SOURCE_DIR = Path(__file__).resolve().parent


def _data_dir():
    """Return the writable directory for config, SQLite state, and logs.

    Source checkouts keep their existing repo-local behavior. A frozen macOS app
    uses Application Support so upgrades replace only the app, never user data.
    EARDITOR_DATA_DIR is intentionally supported for tests and power users.
    """
    override = os.environ.get("EARDITOR_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path.home() / "Library" / "Application Support" / "Earditor"
    return SOURCE_DIR


DATA_DIR = _data_dir()
CONFIG_PATH = Path(os.environ.get("EARDITOR_CONFIG", DATA_DIR / "config.json"))
DB_PATH = DATA_DIR / "earditor.db"

# The database name before Earditor. The first run migrates it in place without
# ever overwriting an existing earditor.db.
_LEGACY_DB_PATHS = (DATA_DIR / "shazamer.db",)


def migrate_db_filename():
    """
    One-time rename of a pre-Earditor database, preserving all processed-track
    history. Never overwrites an existing earditor.db. Also moves the -wal/-shm
    siblings so SQLite keeps any pending WAL data. Called before connections open.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        return
    legacy = next((p for p in _LEGACY_DB_PATHS if p.exists()), None)
    if not legacy:
        return
    os.rename(legacy, DB_PATH)
    for suffix in ("-wal", "-shm"):
        old = Path(f"{legacy}{suffix}")
        if old.exists():
            os.rename(old, Path(f"{DB_PATH}{suffix}"))
    logger.info("migrated %s → %s", legacy.name, DB_PATH.name)

# Defaults — merged under whatever config.json provides.
# NOTE: keep everything here generic/non-personal. The real library location is
# set per-machine in config.json (which is gitignored). See config.example.json.
DEFAULTS = {
    # Where the music library lives. Structure: <music_path>/<Artist>/<Album>/track.mp3
    # On a stock macOS iTunes/Music library this is usually:
    #   ~/Music/Music/Media.localized/Music
    # Override in config.json — this default is just a sensible starting point.
    "music_path": str(Path.home() / "Music"),
    # File extensions to scan.
    "audio_extensions": [".mp3", ".m4a", ".flac", ".wav", ".aac"],
    # Signal thresholds (see verify.py).
    "thresholds": {
        # rapidfuzz token_set_ratio cutoff for "these two strings mean the same thing"
        "fuzzy_match": 85,
        # duration sanity: allowed drift as a fraction OR absolute seconds (whichever is larger)
        "duration_pct": 0.10,
        "duration_abs_sec": 15,
        # AcoustID match score below which we ignore the result entirely
        "acoustid_min_score": 0.5,
    },
    # Politeness delays (seconds) so we don't hammer the free APIs.
    "delays": {
        "shazam": 1.5,
        "musicbrainz": 1.1,
        "itunes": 0.3,
    },
    # Optional LLM tie-breaker for UNVERIFIED. Off by default (spec: never in core pipeline).
    "use_llm_tiebreaker": False,
    "llm_model": "qwen2.5:7b",
    # iTunes playlist name that accepted tracks get added to.
    "playlist_name": "Earditor — Tagged",
    # "Scan for more" scans until this many files reach the review queue, rather
    # than scanning a fixed number of files — most pending files are already
    # clean-tagged and resolve without ever producing a card. scan_max_files is a
    # safety cap so a run can't crawl the whole library looking for cards.
    "scan_queue_target": 10,
    "scan_max_files": 500,
    # Album to stamp on a cover when it has no real album, so the file reads as
    # fully tagged and doesn't loop back into the queue on the next scan (a blank
    # album fails the "already tagged" check). "{artist}" is filled with the cover
    # artist. Set to "" to keep covers album-less (they will re-appear after rescan).
    "cover_album_template": "{artist} (Covers)",
    # Album stamped on an uploader's ORIGINAL song (review option 3) — a work not
    # documented in any catalog. Needed so the file passes the already-tagged check
    # and never re-queues. "{artist}" is filled with the uploader/folder.
    "original_album_template": "{artist} (Originals)",
    # Stamp Grouping=Cover (ID3 TIT1 / MP4 ©grp) on catalog-verified cover artists
    # (review option 1 with a cover signal) so covers are smart-playlist-able while
    # keeping the artist's real album/art. Set false to leave Grouping untouched.
    "stamp_cover_grouping": True,
}


class Config:
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    @property
    def music_path(self):
        # Expand ~ and env vars so config.json can use portable paths.
        return os.path.expanduser(os.path.expandvars(self._data["music_path"]))

    @property
    def audio_extensions(self):
        return tuple(e.lower() for e in self._data["audio_extensions"])

    @property
    def thresholds(self):
        return self._data["thresholds"]

    @property
    def delays(self):
        return self._data["delays"]

    @property
    def playlist_name(self):
        return self._data["playlist_name"]

    @property
    def use_llm_tiebreaker(self):
        return bool(self._data.get("use_llm_tiebreaker", False))

    @property
    def db_path(self):
        return str(DB_PATH)


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=CONFIG_PATH):
    """Load config.json merged over DEFAULTS. Missing file → defaults only."""
    data = dict(DEFAULTS)
    p = Path(path)
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
            data = _deep_merge(DEFAULTS, user)
            if data.get("playlist_name") == "Shazamer — Tagged":
                data["playlist_name"] = DEFAULTS["playlist_name"]
        except Exception as e:
            logger.warning("Could not parse %s (%s); using defaults", path, e)
    else:
        logger.info("No config.json at %s; using built-in defaults", path)
    return Config(data)


def get_acoustid_key():
    """
    Resolve the AcoustID API key without ever persisting it.

    Order: $ACOUSTID_API_KEY, then macOS Keychain (service 'acoustid').
    Also honors $FPCALC by leaving it in the environment for pyacoustid.
    Returns the key string, or None if unavailable (caller degrades gracefully).
    """
    key = os.environ.get("ACOUSTID_API_KEY")
    if key:
        return key.strip()

    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "acoustid", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            key = result.stdout.strip()
            if key:
                logger.debug("AcoustID key loaded from Keychain")
                return key
    except Exception as e:
        logger.debug("Keychain lookup for acoustid failed: %s", e)

    logger.warning("No AcoustID API key found (env or Keychain); AcoustID disabled")
    return None

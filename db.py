#!/usr/bin/env python3
"""
db.py — SQLite state for Earditor.

Replaces V3's exclude.csv + last_index bookkeeping with one durable table.
Scanning is resumable (WHERE status='pending'); accepting/skipping is a status
update. All rich data (each source's raw result, per-signal scores, proposed
tags) is stored as JSON blobs so the review UI can reconstruct the full picture.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path

import config

logger = logging.getLogger("earditor.db")

# Track lifecycle status
STATUS_PENDING = "pending"     # discovered, not yet scanned
STATUS_SCANNED = "scanned"     # scanned, awaiting human review
STATUS_ACCEPTED = "accepted"   # tags written + added to playlist
STATUS_SKIPPED = "skipped"     # human chose to skip
STATUS_NO_MATCH = "no_match"   # no fingerprint anywhere; never rescan
STATUS_TAGGED = "tagged"       # already had complete tags; left untouched, not queued

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    filepath      TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'pending',
    verdict       TEXT,                 -- VERIFIED|LIKELY|COVER|UNVERIFIED|NO_MATCH
    folder_name   TEXT,
    display_name  TEXT,
    duration      REAL,
    proposed_json TEXT,                 -- {title, artist, album, art_url, preview_url}
    shazam_json   TEXT,
    acoustid_json TEXT,
    itunes_json   TEXT,
    candidates_json TEXT,               -- list of candidate options for UNVERIFIED
    options_json  TEXT,                  -- {matched, cover, original, ...} scenario options (§1)
    signals_json  TEXT,                 -- {S1:{...}, ... S6:{...}}
    applied_json  TEXT,                 -- tags actually written on accept (undo keeps proposed_json intact)
    error         TEXT,
    scanned_at    TEXT,
    applied_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_status  ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_verdict ON tracks(verdict);
"""

_migrated = False


def _migrate(conn):
    """Add columns introduced after the original schema (existing DBs)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
    if cols and "applied_json" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN applied_json TEXT")
        conn.commit()
        logger.info("migrated: added tracks.applied_json")
    if cols and "options_json" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN options_json TEXT")
        conn.commit()
        logger.info("migrated: added tracks.options_json")

# Order tiers worst-first for the review queue. ALREADY_TAGGED sits last —
# reviewable but out of the way, since it usually just needs a glance.
VERDICT_ORDER = {"UNVERIFIED": 0, "COVER": 1, "LIKELY": 2, "VERIFIED": 3,
                 "ALREADY_TAGGED": 4, "NO_MATCH": 5}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def connect(db_path):
    global _migrated
    # Only the real application database participates in name migration. Demo and
    # test databases must never mutate a user's local state as a side effect.
    if Path(db_path).resolve() == config.DB_PATH.resolve():
        config.migrate_db_filename()
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if not _migrated:
        try:
            _migrate(conn)
        except Exception as e:          # brand-new DB: table not created yet
            logger.debug("migration skipped: %s", e)
        _migrated = True
    return conn


def init_db(db_path):
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


def _row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    for k in ("proposed", "shazam", "acoustid", "itunes", "candidates", "options", "signals", "applied"):
        raw = d.pop(f"{k}_json", None)
        d[k] = json.loads(raw) if raw else None
    return d


def add_pending(conn, filepath):
    """Register a file as pending if not already known. Returns True if newly added."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO tracks (filepath, status) VALUES (?, ?)",
        (filepath, STATUS_PENDING),
    )
    return cur.rowcount > 0


def add_pending_bulk(conn, filepaths):
    """Batch-register files. Returns count newly added."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO tracks (filepath, status) VALUES (?, 'pending')",
        [(fp,) for fp in filepaths],
    )
    conn.commit()
    return conn.total_changes - before


def get_pending(conn, limit=None):
    q = "SELECT filepath FROM tracks WHERE status = 'pending' ORDER BY filepath"
    if limit:
        q += f" LIMIT {int(limit)}"
    return [r["filepath"] for r in conn.execute(q)]


def count_by_status(conn):
    return {r["status"]: r["n"] for r in
            conn.execute("SELECT status, COUNT(*) n FROM tracks GROUP BY status")}


def count_by_verdict(conn):
    return {r["verdict"]: r["n"] for r in conn.execute(
        "SELECT verdict, COUNT(*) n FROM tracks WHERE status='scanned' GROUP BY verdict")}


def save_scan_result(conn, filepath, result):
    """
    Persist a completed scan. `result` is the dict verify+sources produced:
      verdict, folder_name, display_name, duration, proposed, shazam,
      acoustid, itunes, candidates, signals, error
    Sets status to 'scanned' (or 'no_match' for NO_MATCH verdict).
    """
    verdict = result.get("verdict")
    status = STATUS_NO_MATCH if verdict == "NO_MATCH" else STATUS_SCANNED
    conn.execute(
        """
        UPDATE tracks SET
            status=?, verdict=?, folder_name=?, display_name=?, duration=?,
            proposed_json=?, shazam_json=?, acoustid_json=?, itunes_json=?,
            candidates_json=?, options_json=?, signals_json=?, error=?, scanned_at=?
        WHERE filepath=?
        """,
        (
            status, verdict, result.get("folder_name"), result.get("display_name"),
            result.get("duration"),
            _dumps(result.get("proposed")), _dumps(result.get("shazam")),
            _dumps(result.get("acoustid")), _dumps(result.get("itunes")),
            _dumps(result.get("candidates")), _dumps(result.get("options")),
            _dumps(result.get("signals")),
            result.get("error"), _now(), filepath,
        ),
    )
    conn.commit()


def save_error(conn, filepath, message):
    conn.execute(
        "UPDATE tracks SET error=?, scanned_at=? WHERE filepath=?",
        (str(message)[:1000], _now(), filepath),
    )
    conn.commit()


def get_track(conn, filepath):
    row = conn.execute("SELECT * FROM tracks WHERE filepath=?", (filepath,)).fetchone()
    return _row_to_dict(row)


def get_review_queue(conn, include_tagged=False):
    """
    Tracks awaiting review, worst-verdict-first. Includes already-tagged files
    (as the ALREADY_TAGGED tier) so nothing is hidden from the human — they sort
    to the bottom but stay reviewable.
    """
    statuses = ("scanned", "tagged") if include_tagged else ("scanned",)
    ph = ",".join("?" * len(statuses))
    rows = conn.execute(f"SELECT * FROM tracks WHERE status IN ({ph})", statuses).fetchall()
    out = [d for d in (_row_to_dict(r) for r in rows) if d]
    out.sort(key=lambda d: (VERDICT_ORDER.get(d.get("verdict"), 9),
                            d.get("display_name") or d.get("filepath") or ""))
    return out


def mark_accepted(conn, filepath, final_tags):
    """
    Record an accept. The tags we actually wrote go in applied_json; proposed_json
    (the original scan proposal, plus candidates/signals) is left intact so undo can
    put the track back in the queue exactly as it was.
    """
    conn.execute(
        "UPDATE tracks SET status='accepted', applied_json=?, applied_at=? WHERE filepath=?",
        (_dumps(final_tags), _now(), filepath),
    )
    conn.commit()


def mark_skipped(conn, filepath):
    conn.execute("UPDATE tracks SET status='skipped', applied_at=? WHERE filepath=?",
                 (_now(), filepath))
    conn.commit()


def applied_tags(row_dict):
    """Tags actually written for a handled track (legacy rows kept them in proposed)."""
    return row_dict.get("applied") or row_dict.get("proposed") or {}


def undo_action(conn, filepath):
    """
    Put an accepted/skipped track back in the review queue. Returns
    {prev_status, applied} or None if there was nothing to undo.

    Note: this does NOT rewrite the file's tags — an accept already wrote them and
    Music.app may have moved the file. Undo re-opens the track so you can correct it;
    re-accepting overwrites the tags with the right ones.
    """
    row = conn.execute(
        "SELECT status, applied_json, proposed_json FROM tracks WHERE filepath=?",
        (filepath,),
    ).fetchone()
    if not row or row["status"] not in (STATUS_ACCEPTED, STATUS_SKIPPED):
        return None
    prev = row["status"]
    conn.execute(
        "UPDATE tracks SET status=?, applied_at=NULL WHERE filepath=?",
        (STATUS_SCANNED, filepath),
    )
    conn.commit()
    raw = row["applied_json"] or row["proposed_json"]
    return {"prev_status": prev, "applied": json.loads(raw) if raw else None}


def recent_actions(conn, limit=20):
    """Most recently accepted/skipped tracks, newest first — the undo history."""
    rows = conn.execute(
        "SELECT * FROM tracks WHERE status IN (?, ?) AND applied_at IS NOT NULL "
        "ORDER BY applied_at DESC LIMIT ?",
        (STATUS_ACCEPTED, STATUS_SKIPPED, int(limit)),
    ).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        out.append({
            "filepath": d["filepath"],
            "status": d["status"],
            "verdict": d["verdict"],
            "display_name": d.get("display_name"),
            "tags": applied_tags(d),
            "at": d.get("applied_at"),
        })
    return out


def relocate(conn, old_path, new_path):
    """Point a track row at a new file path (Music.app moves files after tagging)."""
    if old_path == new_path:
        return
    conn.execute("DELETE FROM tracks WHERE filepath=?", (new_path,))
    conn.execute("UPDATE tracks SET filepath=? WHERE filepath=?", (new_path, old_path))
    conn.commit()


def mark_tagged(conn, filepath, tags):
    """Record a file that already had complete tags — skip review, don't rescan."""
    conn.execute(
        "UPDATE tracks SET status='tagged', verdict='ALREADY_TAGGED', proposed_json=?, scanned_at=? "
        "WHERE filepath=?",
        (_dumps(tags), _now(), filepath),
    )
    conn.commit()


def mark_tagged_bulk(conn, rows):
    """Retire many already-tagged files at once. rows = [(filepath, tags_dict), ...]"""
    now = _now()
    conn.executemany(
        "UPDATE tracks SET status='tagged', verdict='ALREADY_TAGGED', proposed_json=?, "
        "scanned_at=? WHERE filepath=?",
        [(_dumps(tags), now, fp) for fp, tags in rows],
    )
    conn.commit()


def mark_accepted_from_csv(conn, filepath):
    """Used by migrate_v3: record an already-processed file so it's never rescanned."""
    conn.execute(
        "INSERT INTO tracks (filepath, status, verdict, applied_at) VALUES (?, 'accepted', 'VERIFIED', ?) "
        "ON CONFLICT(filepath) DO UPDATE SET status='accepted'",
        (filepath, _now()),
    )

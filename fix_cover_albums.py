#!/usr/bin/env python3
"""
fix_cover_albums.py — stamp a "covers" album on album-less covers so they stop
looping back into the review queue.

A cover accepted with a blank album fails the scan's "already tagged" check
(title + artist + album), so after Music.app relocates the file the next scan
re-discovers it and drops it back in the queue — forever. This fills in a synthetic
album from `cover_album_template` in config.json (default "{artist} (Covers)").

It only touches REAL covers — tracks where an accepted artist differs from what the
audio fingerprint identified (i.e. the artist was deliberately reassigned to the
uploader), or that were classified COVER. Legit originals that merely lack an album
(e.g. YOASOBI — Monster, AiSS — Truly) are left alone. Runs through Music.app so it
reaches each file at its current location. Idempotent.

Usage:
    python3 fix_cover_albums.py             # fix them
    python3 fix_cover_albums.py --dry-run   # list what would change, change nothing
"""

import argparse
import json
import logging
import subprocess
import sys

import db
from config import DB_PATH, load_config
from verify import ratio

log = logging.getLogger("earditor.fix_cover_albums")

SEP = " |§| "          # key separator unlikely to appear in a title/artist
_FUZZY = 85


def _q(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def cover_keys():
    """
    Return the set of "title<SEP>artist" for accepted, album-less REAL covers.
    A cover = the accepted artist disagrees with the fingerprint's artist (it was
    reassigned to the uploader), or the track was classified COVER.
    """
    conn = db.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT verdict, applied_json, proposed_json, shazam_json, acoustid_json "
            "FROM tracks WHERE status='accepted' "
            "AND (applied_json IS NOT NULL OR proposed_json IS NOT NULL)"
        ).fetchall()
    finally:
        conn.close()

    keys = set()
    for r in rows:
        # The tags we actually wrote live in applied_json (legacy rows: proposed_json).
        p = json.loads(r["applied_json"] or r["proposed_json"])
        album = (p.get("album") or "").strip()
        artist = (p.get("artist") or "").strip()
        title = (p.get("title") or "").strip()
        if album or not artist or not title:
            continue
        fp_artist = None
        for k in ("shazam_json", "acoustid_json"):
            if r[k]:
                fp_artist = (json.loads(r[k]).get("artist") or "").strip() or fp_artist
        is_cover = (r["verdict"] == "COVER") or (fp_artist and ratio(fp_artist, artist) < _FUZZY)
        if is_cover:
            keys.add(title + SEP + artist)
    return keys


def apply(playlist, template, keys, dry_run):
    if not keys:
        log.info("No album-less covers found — nothing to do.")
        return 0
    if "{artist}" in template:
        prefix, suffix = template.split("{artist}", 1)
        album_expr = f'"{_q(prefix)}" & a & "{_q(suffix)}"'
    else:
        album_expr = f'"{_q(template)}"'
    keys_lit = ", ".join(f'"{_q(k)}"' for k in keys)
    set_line = "" if dry_run else "                        set album of t to newAlbum\n"
    script = f'''
    tell application "Music"
        try
            set pl to playlist "{_q(playlist)}"
        on error
            return "NO_PLAYLIST"
        end try
        set coverKeys to {{{keys_lit}}}
        set n to 0
        set report to ""
        repeat with t in (every track of pl)
            try
                if (album of t) is "" then
                    set a to (artist of t)
                    set theKey to (name of t) & "{_q(SEP)}" & a
                    if theKey is in coverKeys then
                        set newAlbum to {album_expr}
{set_line}                        set n to n + 1
                        set report to report & a & " — " & (name of t) & "  ⇒  " & newAlbum & linefeed
                    end if
                end if
            end try
        end repeat
        return (n as string) & linefeed & report
    end tell
    '''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=180)
    except Exception as e:
        log.error("osascript failed: %s", e)
        return 1
    out = (r.stdout or "").strip()
    if out == "NO_PLAYLIST":
        log.error("Playlist %r not found in Music.app.", playlist)
        return 1
    if r.returncode != 0:
        log.error("osascript error: %s", (r.stderr or "").strip())
        return 1
    lines = out.splitlines()
    for line in lines[1:]:
        if line.strip():
            log.info("  %s", line)
    verb = "Would stamp" if dry_run else "Stamped"
    log.info("%s a covers-album on %s track(s).", verb, lines[0] if lines else "0")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Stamp a covers-album on album-less covers.")
    ap.add_argument("--playlist", default=None, help="playlist to fix (default: config playlist_name)")
    ap.add_argument("--dry-run", action="store_true", help="list changes without applying them")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    cfg = load_config()
    template = (cfg.get("cover_album_template", "") or "").strip()
    if not template:
        log.info("cover_album_template is empty in config.json — nothing to do.")
        return 0
    playlist = args.playlist or cfg.playlist_name
    keys = cover_keys()
    log.info("%s %d album-less cover(s) in “%s” using template %r%s",
             "Found" if args.dry_run else "Fixing", len(keys), playlist, template,
             "  (dry run)" if args.dry_run else "")
    return apply(playlist, template, keys, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
refresh_artwork.py — force Music.app to reload embedded cover art.

Music.app caches artwork in its own library database. A plain AppleScript
`refresh` re-reads a track's text tags but NOT its artwork, so a track whose file
got a new embedded cover (e.g. anything tagged by the Earditor review UI) can keep
showing the OLD art in Music.app. This tool re-pushes each track's *embedded* image
straight into Music.app's artwork, which is what actually updates the display.

By default it refreshes every track in the "Earditor — Tagged" playlist — i.e. all
the tracks the review UI has tagged. Use --files to target specific files instead.

Usage:
    python3 refresh_artwork.py                   # all Earditor-tagged tracks
    python3 refresh_artwork.py --limit 20        # first 20 (quick test)
    python3 refresh_artwork.py --files a.mp3 b.m4a
    python3 refresh_artwork.py --playlist "Some Playlist"
    python3 refresh_artwork.py -v                # DEBUG logging
"""

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

from config import load_config
from tagger import extract_embedded_art
from utils import read_existing_tags

log = logging.getLogger("earditor.refresh_artwork")

_BATCH = 50          # tracks per set-artwork osascript call
_AS_TIMEOUT = 120    # seconds


def _uploader_album_re(cfg):
    """
    Regex matching albums stamped on uploader COVERS / ORIGINALS (review options
    2 & 3), derived from the album templates. These tracks intentionally have NO
    Earditor-written art — they keep the file's own thumbnail — so refresh_artwork
    must leave their art alone (§5) rather than re-push it into Music.app.
    """
    pats = []
    for tpl in (cfg.get("cover_album_template", "{artist} (Covers)"),
                cfg.get("original_album_template", "{artist} (Originals)")):
        if not tpl or "{artist}" not in tpl:
            continue
        left, right = tpl.split("{artist}", 1)
        pats.append(re.escape(left) + r".+" + re.escape(right))
    if not pats:
        return None
    return re.compile(r"^(?:%s)$" % "|".join(pats), re.IGNORECASE)


def _is_uploader_tagged(path, album_re):
    """True if the file's album marks it an uploader cover/original (skip its art)."""
    if not album_re:
        return False
    album = (read_existing_tags(path).get("album") or "").strip()
    return bool(album and album_re.match(album))


def _osascript(script):
    """Run an AppleScript; return (ok, stdout)."""
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=_AS_TIMEOUT)
        if r.returncode != 0:
            return False, (r.stderr or "").strip()
        return True, r.stdout.strip()
    except Exception as e:
        return False, str(e)


def _q(s):
    """Escape a string for an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def enumerate_playlist(playlist):
    """Return [(persistent_id, posix_path), ...] for tracks in `playlist`."""
    script = f'''
    tell application "Music"
        try
            set pl to playlist "{_q(playlist)}"
        on error
            return "NO_PLAYLIST"
        end try
        set out to ""
        repeat with t in (every track of pl)
            try
                set loc to (location of t)
                if loc is not missing value then
                    set out to out & (persistent ID of t) & tab & (POSIX path of loc) & linefeed
                end if
            end try
        end repeat
        return out
    end tell
    '''
    ok, out = _osascript(script)
    if not ok:
        log.error("Could not read Music.app: %s", out)
        return None
    if out == "NO_PLAYLIST":
        log.error("Playlist %r not found in Music.app.", playlist)
        return None
    pairs = []
    for line in out.splitlines():
        if "\t" in line:
            pid, path = line.split("\t", 1)
            pairs.append((pid.strip(), path.strip()))
    return pairs


def _set_artwork_by_pid(playlist, batch):
    """batch = [(pid, art_file)]; set each track's artwork. Returns count set."""
    pairs_lit = ", ".join('{"%s", "%s"}' % (_q(pid), _q(af)) for pid, af in batch)
    script = f'''
    tell application "Music"
        set pl to playlist "{_q(playlist)}"
        set n to 0
        set pairs to {{{pairs_lit}}}
        repeat with p in pairs
            try
                set trk to (first track of pl whose persistent ID is (item 1 of p))
                set data of artwork 1 of trk to (read (POSIX file (item 2 of p)) as picture)
                set n to n + 1
            end try
        end repeat
        return (n as string)
    end tell
    '''
    ok, out = _osascript(script)
    if not ok:
        log.warning("  batch failed: %s", out)
        return 0
    try:
        return int(out)
    except ValueError:
        return 0


def _set_artwork_by_location(path, art_file):
    """Set artwork for the library track whose file is `path`. Returns True/False."""
    script = f'''
    tell application "Music"
        try
            set trk to (first track of library playlist 1 whose location is (POSIX file "{_q(path)}"))
            set data of artwork 1 of trk to (read (POSIX file "{_q(art_file)}") as picture)
            return "ok"
        on error err_msg
            return "error: " & err_msg
        end try
    end tell
    '''
    ok, out = _osascript(script)
    return ok and out == "ok"


def _extract_to(tmpdir, key, path):
    """Extract embedded art from `path` to a temp file named by `key`. Returns file or None."""
    data, mime = extract_embedded_art(path)
    if not data:
        return None
    ext = ".png" if (mime and "png" in mime.lower()) else ".jpg"
    dst = os.path.join(tmpdir, f"{key}{ext}")
    with open(dst, "wb") as f:
        f.write(data)
    return dst


def refresh_playlist(playlist, limit=None, album_re=None):
    pairs = enumerate_playlist(playlist)
    if pairs is None:
        return 1
    if limit:
        pairs = pairs[:limit]
    log.info("Found %d track(s) in “%s”.", len(pairs), playlist)

    tmpdir = tempfile.mkdtemp(prefix="earditor_artrefresh_")
    updated = no_art = skipped = 0
    try:
        batch = []
        for pid, path in pairs:
            if os.path.isfile(path) and _is_uploader_tagged(path, album_re):
                skipped += 1
                log.debug("  skip cover/original (keeps own art): %s", os.path.basename(path))
                continue
            art = _extract_to(tmpdir, pid, path) if os.path.isfile(path) else None
            if not art:
                no_art += 1
                log.debug("  no embedded art: %s", os.path.basename(path))
                continue
            batch.append((pid, art))
            if len(batch) >= _BATCH:
                updated += _set_artwork_by_pid(playlist, batch)
                log.info("  …refreshed %d/%d", updated, len(pairs))
                batch = []
        if batch:
            updated += _set_artwork_by_pid(playlist, batch)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    log.info("Done. Artwork refreshed for %d track(s); %d had no embedded art; "
             "%d cover/original tracks left untouched.", updated, no_art, skipped)
    return 0


def refresh_files(paths, album_re=None):
    tmpdir = tempfile.mkdtemp(prefix="earditor_artrefresh_")
    updated = missing = no_art = skipped = 0
    try:
        for i, path in enumerate(paths):
            path = os.path.abspath(os.path.expanduser(path))
            if not os.path.isfile(path):
                log.warning("  not found: %s", path)
                missing += 1
                continue
            if _is_uploader_tagged(path, album_re):
                skipped += 1
                log.info("  ⤫ cover/original — leaving its own art: %s", os.path.basename(path))
                continue
            art = _extract_to(tmpdir, str(i), path)
            if not art:
                log.warning("  no embedded art: %s", os.path.basename(path))
                no_art += 1
                continue
            if _set_artwork_by_location(path, art):
                updated += 1
                log.info("  ✅ %s", os.path.basename(path))
            else:
                log.warning("  ⚠ not found in Music.app (moved?): %s", os.path.basename(path))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    log.info("Done. Refreshed %d; %d missing; %d without embedded art; %d cover/original skipped.",
             updated, missing, no_art, skipped)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Force Music.app to reload embedded cover art.")
    ap.add_argument("--playlist", default=None, help="playlist to refresh (default: config playlist_name)")
    ap.add_argument("--files", nargs="+", default=None, help="refresh specific files instead of a playlist")
    ap.add_argument("--limit", type=int, default=None, help="only the first N playlist tracks (testing)")
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")

    if sys.platform != "darwin":
        log.error("refresh_artwork.py pushes art into Music.app through AppleScript "
                  "and only runs on macOS. Elsewhere the art is already embedded in "
                  "the file itself — no refresh is needed.")
        return 2

    cfg = load_config()
    album_re = _uploader_album_re(cfg)
    if args.files:
        return refresh_files(args.files, album_re=album_re)
    playlist = args.playlist or cfg.playlist_name
    return refresh_playlist(playlist, limit=args.limit, album_re=album_re)


if __name__ == "__main__":
    sys.exit(main())

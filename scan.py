#!/usr/bin/env python3
"""
scan.py — headless scan pipeline for Earditor.

Walks the library, fingerprints each file (Shazam + AcoustID), cross-verifies
against iTunes and the file itself, scores signals S1-S6, assigns a verdict, and
writes everything to SQLite. Unattended + resumable (Ctrl-C safe; rerun to
continue from WHERE status='pending').

Usage:
    python3 scan.py                     # scan all pending files
    python3 scan.py --limit 10          # scan the next 10 (great for testing)
    python3 scan.py --files a.mp3 b.mp3 # scan specific files (adds them if new)
    python3 scan.py --music-path DIR    # override library path for this run
    python3 scan.py --no-acoustid       # skip AcoustID (Shazam-only)
    python3 scan.py -v                  # DEBUG logging
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from config import load_config, get_acoustid_key, DB_PATH
from covers import is_neutral_channel
import db
from sources.shazam import ShazamSource
from sources.acoustid_mb import AcoustIDSource
from sources.itunes import iTunesSource
from tagger import apply_tags
from utils import (filename_tokens, extract_folder_name, raw_folder_name,
                   parse_filename_for_display, get_duration, read_existing_tags,
                   is_well_tagged, tags_look_messy)
from verify import verify

LOG_DIR = Path(__file__).resolve().parent / "logs"

# Verdicts that actually put a card in the review queue. Everything else
# (ALREADY_TAGGED, NO_MATCH, AUTO_ACCEPTED, ERROR) resolves without human input —
# which is why "scan 40 files" can yield only 1 card. See --queue-target.
QUEUEABLE = {"VERIFIED", "LIKELY", "COVER", "UNVERIFIED"}


def setup_logging(verbose=False):
    LOG_DIR.mkdir(exist_ok=True)
    logfile = LOG_DIR / f"scan_{time.strftime('%Y%m%d_%H%M%S')}.log"
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    root = logging.getLogger("earditor")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(fh)
    root.addHandler(ch)
    return logging.getLogger("earditor.scan"), logfile


def walk_library(music_path, extensions):
    for dirpath, _dirs, files in os.walk(music_path):
        for name in files:
            if name.startswith("."):
                continue
            if name.lower().endswith(tuple(extensions)):
                yield os.path.join(dirpath, name)


def gather_evidence(filepath, shazam_src, acoustid_src, itunes_src, cfg, log):
    """Independently collect all evidence for one file."""
    tokens = filename_tokens(filepath)
    folder = extract_folder_name(filepath)
    folder_raw = raw_folder_name(filepath)   # keeps a trailing "- Topic" for the fast-pass
    file_dur = get_duration(filepath)

    sh = shazam_src.recognize(filepath) if shazam_src else None
    time.sleep(cfg.delays["shazam"])
    if sh:
        log.info("  Shazam:   %s — %s", sh.get("title"), sh.get("artist"))
    else:
        log.info("  Shazam:   (no match)")

    ac = acoustid_src.identify(filepath) if acoustid_src and acoustid_src.enabled else None
    if ac:
        log.info("  AcoustID: %s — %s  (score %.2f)",
                 ac.get("title"), ac.get("artist"), ac.get("score", 0))
    else:
        log.info("  AcoustID: (no match / disabled)")

    primary_title = (sh or {}).get("title") or (ac or {}).get("title")
    primary_artist = (sh or {}).get("artist") or (ac or {}).get("artist")

    itunes = None
    itunes_candidates = []
    itunes_folder = None
    if primary_title:
        itunes = itunes_src.search_track(primary_artist, primary_title)
        itunes_candidates = itunes_src.search_candidates(primary_title, limit=5)
        if itunes:
            log.info("  iTunes:   %s — %s  | album: %s",
                     itunes.get("title"), itunes.get("artist"), itunes.get("album"))

        # Does the FOLDER artist have their own catalog release of this title?
        # Covers are the hard case: many artists release the same song, and a
        # fingerprint can confidently name the wrong one. The folder says whose
        # upload this is, so their release is strong evidence. (Skip label/auto
        # channels — "VEVO" isn't an artist to look up.)
        if folder and not is_neutral_channel(folder):
            itunes_folder = itunes_src.search_track(folder, primary_title)
            if itunes_folder:
                log.info("  iTunes:   folder artist '%s' also has this title — %s | album: %s",
                         folder, itunes_folder.get("artist"), itunes_folder.get("album"))

    return {
        "file_duration": file_dur,
        "folder_name": folder,
        "folder_raw": folder_raw,
        "filename": tokens["raw"],
        "filename_original": tokens.get("original"),
        "tokens": tokens,
        "shazam": sh,
        "acoustid": ac,
        "itunes": itunes,
        "itunes_candidates": itunes_candidates,
        "itunes_folder": itunes_folder,
    }


def _sig_flag(sig):
    return {"green": "✓", "yellow": "~", "red": "✗", "neutral": "·"}.get(sig["status"], "?")


def triage(conn, log, limit=None, chunk=500):
    """
    Fast pass: tag read only — no fingerprinting, no API calls, no delays.

    Retires every pending file that already has clean title+artist+album as
    ALREADY_TAGGED. Roughly half a real library is like this, and scanning them
    one-at-a-time through the full pipeline only to skip them is why the pending
    count looks terrifying. After a triage run, `pending` means "files that
    actually need identifying".
    """
    pending = db.get_pending(conn, limit=limit)
    log.info("Triaging %d pending file(s) — reading tags only…", len(pending))
    batch = []
    retired = messy = untagged = missing = 0
    for i, fp in enumerate(pending, 1):
        if not os.path.isfile(fp):
            missing += 1
            continue
        existing = read_existing_tags(fp)
        if is_well_tagged(fp, existing):
            if tags_look_messy(fp, existing):
                messy += 1                      # needs re-identifying; leave pending
            else:
                batch.append((fp, existing))
                retired += 1
        else:
            untagged += 1
        if len(batch) >= chunk:
            db.mark_tagged_bulk(conn, batch)
            batch = []
            log.info("  …%d/%d scanned, %d retired", i, len(pending), retired)
    if batch:
        db.mark_tagged_bulk(conn, batch)

    log.info("Triage done. Retired %d already-tagged file(s).", retired)
    log.info("Left to identify: %d  (%d untagged + %d with messy tags)%s",
             untagged + messy, untagged, messy,
             f"; {missing} missing" if missing else "")
    return retired


def _auto_accept_topic(filepath, result, evidence, cfg, conn, log):
    """
    Apply tags for a Topic-channel fast-pass match, the same way the review UI does
    on Accept (write ID3 + art, add to the Music.app playlist, mark accepted).
    Returns the status string on success, or None if the tag write failed (caller
    then saves the track for manual review so nothing is lost).
    """
    p = result.get("proposed") or {}
    tags = {"title": p.get("title"), "artist": p.get("artist"),
            "album": p.get("album"), "art_url": p.get("art_url")}
    log.info("  ► TOPIC AUTO-ACCEPT — folder '%s' is an official '- Topic' channel and "
             "the candidate artist matches; accepting without review",
             evidence.get("folder_name"))
    if apply_tags(filepath, tags, cfg.playlist_name):
        db.mark_accepted(conn, filepath, tags)
        log.info("  ✅ AUTO-ACCEPTED: %s — %s | album: %s  (never entered the review queue)",
                 tags["artist"], tags["title"], tags.get("album") or "—")
        return "AUTO_ACCEPTED"
    log.warning("  ⚠ auto-accept tag write FAILED for %s — saving for manual review instead",
                os.path.basename(filepath))
    return None


def scan_one(filepath, sources, cfg, conn, log, skip_tagged=True, auto_accept=True):
    shazam_src, acoustid_src, itunes_src = sources
    display = parse_filename_for_display(filepath)
    log.info("── %s", os.path.basename(filepath))
    try:
        # Fast path: files that already have CLEAN title+artist+album are official /
        # already-processed — skip them. But files whose existing tags look messy
        # (mojibake, title = raw filename, video junk) still get re-identified so we
        # can propose a clean tag — those are exactly the ones worth reviewing.
        if skip_tagged:
            existing = read_existing_tags(filepath)
            if is_well_tagged(filepath, existing):
                if tags_look_messy(filepath, existing):
                    log.info("  ► tagged but MESSY (%s) — re-identifying",
                             existing["title"])
                else:
                    log.info("  ► ALREADY TAGGED (clean) — %s — %s (skipping)",
                             existing["artist"], existing["title"])
                    db.mark_tagged(conn, filepath, existing)
                    return "ALREADY_TAGGED"

        evidence = gather_evidence(filepath, shazam_src, acoustid_src, itunes_src, cfg, log)
        result = verify(evidence, cfg.thresholds)
        sig = result["signals"]
        log.info(
            "  ► %s (score %d) | S1%s S2%s S3%s S4%s S5%s S6%s",
            result["verdict"], result["score"],
            _sig_flag(sig["S1"]), _sig_flag(sig["S2"]), _sig_flag(sig["S3"]),
            _sig_flag(sig["S4"]), _sig_flag(sig["S5"]), _sig_flag(sig["S6"]),
        )
        for s in sig.values():
            log.debug("      %s %s: %s", s["id"], s["status"], s["explain"])

        # Topic-channel fast-pass: an official "Artist - Topic" upload whose
        # candidate artist matches the channel. Auto-accept it (same path as
        # accepting in review) so it never lands in the manual queue.
        if result.get("auto_accept"):
            if not auto_accept:
                log.info("  ► Topic match — auto-accept disabled, queueing for review instead")
            else:
                verdict = _auto_accept_topic(filepath, result, evidence, cfg, conn, log)
                if verdict:
                    return verdict
                # apply_tags failed — fall through and save for manual review instead.

        record = {
            "verdict": result["verdict"],
            "folder_name": evidence["folder_name"],
            "display_name": display,
            "duration": evidence["file_duration"],
            "proposed": result["proposed"],
            "shazam": evidence["shazam"],
            "acoustid": evidence["acoustid"],
            "itunes": evidence["itunes"],
            "candidates": result["candidates"],
            "options": result.get("options"),
            "signals": result["signals"],
            "error": None,
        }
        db.save_scan_result(conn, filepath, record)
        return result["verdict"]
    except Exception as e:
        log.exception("  ERROR scanning %s: %s", filepath, e)
        db.save_error(conn, filepath, e)
        return "ERROR"


def main():
    ap = argparse.ArgumentParser(description="Earditor — scan pipeline")
    ap.add_argument("--limit", type=int, default=None, help="scan at most N pending files")
    ap.add_argument("--queue-target", type=int, default=None,
                    help="keep scanning until N files land in the review queue")
    ap.add_argument("--max-files", type=int, default=None,
                    help="hard cap on files scanned (use with --queue-target)")
    ap.add_argument("--triage", action="store_true",
                    help="fast pass: retire already-tagged files without fingerprinting, then exit")
    ap.add_argument("--files", nargs="+", default=None, help="scan specific file paths")
    ap.add_argument("--music-path", default=None, help="override library path")
    ap.add_argument("--no-acoustid", action="store_true", help="disable AcoustID")
    ap.add_argument("--rescan-tagged", action="store_true",
                    help="also re-identify files that already have complete tags")
    ap.add_argument("--no-auto-accept", action="store_true",
                    help="read-only: never write tags or touch Music.app; Topic-channel "
                         "matches become review cards instead of auto-accepting")
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")
    args = ap.parse_args()

    log, logfile = setup_logging(args.verbose)
    cfg = load_config()
    music_path = args.music_path or cfg.music_path
    log.info("Earditor scan — db=%s  log=%s", DB_PATH, logfile)

    conn = db.init_db(str(DB_PATH))

    # Register work.
    if args.files:
        files = [str(Path(f).resolve()) for f in args.files]
        added = db.add_pending_bulk(conn, files)
        log.info("Registered %d explicit file(s) (%d new)", len(files), added)
        pending = files
    else:
        if not os.path.isdir(music_path):
            log.error("Music path does not exist: %s", music_path)
            sys.exit(1)
        log.info("Walking library: %s", music_path)
        all_files = list(walk_library(music_path, cfg.audio_extensions))
        added = db.add_pending_bulk(conn, all_files)
        log.info("Found %d audio files (%d newly registered)", len(all_files), added)

        if args.triage:
            triage(conn, log, limit=args.limit)
            log.info("DB status counts: %s", db.count_by_status(conn))
            conn.close()
            return

        # With --queue-target we need a deep enough pool: most pending files are
        # already clean-tagged and resolve without ever producing a card.
        fetch_limit = args.max_files if args.queue_target else args.limit
        pending = db.get_pending(conn, limit=fetch_limit)

    if args.limit:
        pending = pending[: args.limit]

    if args.queue_target:
        log.info("Scanning until %d file(s) reach the review queue (max %s file(s))…",
                 args.queue_target, args.max_files or "unlimited")
    else:
        log.info("Scanning %d pending file(s)…", len(pending))

    shazam_src = ShazamSource()
    acoustid_key = None if args.no_acoustid else get_acoustid_key()
    acoustid_src = AcoustIDSource(acoustid_key, cfg.thresholds["acoustid_min_score"])
    itunes_src = iTunesSource(cfg.delays["itunes"])
    sources = (shazam_src, acoustid_src, itunes_src)

    tally = {}
    queued = 0
    scanned = 0
    try:
        for i, fp in enumerate(pending, 1):
            if args.max_files and i > args.max_files:
                log.info("Hit --max-files cap of %d.", args.max_files)
                break
            if args.queue_target:
                log.info("[file %d | %d/%d queued]", i, queued, args.queue_target)
            else:
                log.info("[%d/%d]", i, len(pending))
            verdict = scan_one(fp, sources, cfg, conn, log,
                               skip_tagged=not args.rescan_tagged,
                               auto_accept=not args.no_auto_accept)
            tally[verdict] = tally.get(verdict, 0) + 1
            scanned = i
            if verdict in QUEUEABLE:
                queued += 1
            if args.queue_target and queued >= args.queue_target:
                log.info("Queue target reached: %d card(s) after scanning %d file(s).",
                         queued, i)
                break
    except KeyboardInterrupt:
        log.warning("Interrupted — progress saved. Rerun to resume.")

    log.info("Done. Scanned %d file(s) → %d new review card(s).", scanned, queued)
    log.info("Verdicts this run: %s", tally)
    log.info("DB status counts: %s", db.count_by_status(conn))
    conn.close()


if __name__ == "__main__":
    main()

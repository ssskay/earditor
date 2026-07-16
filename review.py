#!/usr/bin/env python3
"""
review.py — local web UI for reviewing scan results and applying tags.

Reads from earditor.db (populated by scan.py). Presents a queue grouped by
verdict tier, worst-first, with an evidence panel and a listen-and-compare
player (local file + iTunes 30s preview). Accepting a track writes ID3 tags +
art and adds it to the Music.app playlist.

Run:  python3 review.py           # http://127.0.0.1:5000
"""

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests
from flask import Flask, render_template, jsonify, request, send_file, abort

import db
import verify
from config import load_config, DB_PATH
from tagger import apply_tags
from itunes_bridge import find_track_location
from sources.itunes import iTunesSource

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("earditor.review")

app = Flask(__name__)
cfg = load_config()
itunes = iTunesSource(delay=0)

HERE = Path(__file__).resolve().parent
RESOURCE_ROOT = Path(os.environ.get("RESOURCEPATH", HERE))
app.template_folder = str(RESOURCE_ROOT / "templates")

# Demo mode (python3 review.py --demo): loads synthetic fixtures into a throwaway
# demo.db, writes nothing to real files or Music.app, and needs no config or keys.
# Set in __main__ before the server starts, then read as a global by the routes.
DEMO = False
DEMO_DB_PATH = HERE / "demo.db"
DEMO_FIXTURES = RESOURCE_ROOT / "demo" / "fixtures.json"

def _cfg_int(key, default):
    try:
        return int(cfg.get(key, default))
    except Exception:
        return default


# "Scan for more" scans until this many review cards appear (not a fixed file count):
# most pending files are already clean-tagged and never produce a card.
SCAN_QUEUE_TARGET = _cfg_int("scan_queue_target", 10)
SCAN_MAX_FILES = _cfg_int("scan_max_files", 500)

TIER_ORDER = ["UNVERIFIED", "COVER", "LIKELY", "VERIFIED", "ALREADY_TAGGED"]


def conn():
    return db.connect(str(DB_PATH))


# ---------------------------------------------------------------------------
# Background scan job — runs scan.py as a subprocess so "Scan for more" works
# from the UI without a terminal. Single job at a time (guarded by a lock);
# progress is derived from the live pending count in the DB.
# ---------------------------------------------------------------------------
_scan_lock = threading.Lock()
SCAN = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "total": 0,        # review cards this run is aiming for
    "done": 0,         # review cards produced so far this run
    "scanned": 0,      # files chewed through (most auto-skip)
    "returncode": None,
    "error": None,
}


def _status_counts():
    c = conn()
    try:
        return db.count_by_status(c)
    finally:
        c.close()


def _scan_worker():
    """
    Run scan.py until it produces SCAN_QUEUE_TARGET review cards. Progress is the
    number of NEW cards ('scanned' rows) — not the raw pending count, which is
    every audio file in the library and mostly resolves without a card.
    """
    def _resolved(counts):
        # Files that have left 'pending'. Robust to the library walk ADDING pending
        # rows mid-run (moved/new files), which made `start_pending - pending` go 0.
        return sum(v for k, v in counts.items() if k != "pending")

    before = _status_counts()
    start_cards = before.get("scanned", 0)
    start_resolved = _resolved(before)
    with _scan_lock:
        SCAN["total"] = SCAN_QUEUE_TARGET
        SCAN["done"] = 0
        SCAN["scanned"] = 0
    scan_args = ["--queue-target", str(SCAN_QUEUE_TARGET),
                 "--max-files", str(SCAN_MAX_FILES)]
    try:
        if getattr(sys, "frozen", False):
            # A py2app bundle has no standalone scan.py to launch. Reuse the same
            # CLI entry point in a nested worker so this thread can keep publishing
            # live progress while the native Scan button does its work.
            import scan
            result = {"returncode": None}
            scan_thread = threading.Thread(
                target=lambda: result.update(returncode=scan.main(scan_args)),
                daemon=True,
            )
            scan_thread.start()
            proc = None
        else:
            proc = subprocess.Popen(
                [sys.executable, str(HERE / "scan.py"), *scan_args],
                cwd=str(HERE),
            )
            returncode = None
    except Exception as e:
        with _scan_lock:
            SCAN.update(running=False, error=str(e), finished_at=time.time())
        log.exception("Failed to launch scan subprocess: %s", e)
        return

    if proc is not None or getattr(sys, "frozen", False):
        while ((proc is not None and proc.poll() is None) or
               (proc is None and scan_thread.is_alive())):
            counts = _status_counts()
            cards = max(0, counts.get("scanned", 0) - start_cards)
            files = max(0, _resolved(counts) - start_resolved)
            with _scan_lock:
                SCAN["done"] = min(SCAN["total"], cards)
                SCAN["scanned"] = files
            time.sleep(1.0)
        returncode = proc.returncode if proc is not None else result["returncode"]

    with _scan_lock:
        SCAN["returncode"] = returncode
        SCAN["done"] = max(0, _status_counts().get("scanned", 0) - start_cards)
        SCAN["running"] = False
        SCAN["finished_at"] = time.time()
        if returncode not in (0, None):
            SCAN["error"] = f"scan exited with code {returncode}"
    log.info("Background scan finished (returncode=%s, new cards=%s)",
             returncode, SCAN["done"])


def _scan_snapshot():
    """Header counts + scan progress, cheap enough to poll every ~1.5s."""
    c = conn()
    try:
        status_counts = db.count_by_status(c)
        tier_counts = db.count_by_verdict(c)
    finally:
        c.close()
    with _scan_lock:
        s = dict(SCAN)
    return {
        "running": s["running"],
        "total": s["total"],          # cards this run is aiming for
        "done": s["done"],            # cards produced so far
        "scanned": s["scanned"],      # files chewed through (most auto-skip)
        "pending": status_counts.get("pending", 0),   # raw file backlog, NOT cards
        "error": s["error"],
        "tier_counts": tier_counts,
        "status_counts": status_counts,
    }


@app.route("/api/scan/start", methods=["POST"])
def api_scan_start():
    if DEMO:
        return jsonify({"ok": False, "error": "demo"}), 400
    with _scan_lock:
        if SCAN["running"]:
            return jsonify({"ok": False, "error": "already_running"}), 409
        SCAN.update(running=True, started_at=time.time(), finished_at=None,
                    total=0, done=0, returncode=None, error=None)
    threading.Thread(target=_scan_worker, daemon=True).start()
    log.info("Background scan started")
    return jsonify({"ok": True})


@app.route("/api/scan/status")
def api_scan_status():
    return jsonify(_scan_snapshot())


@app.route("/api/recent")
def api_recent():
    """Recently accepted/skipped tracks — the undo history."""
    c = conn()
    try:
        items = db.recent_actions(c, limit=int(request.args.get("limit", 20)))
    finally:
        c.close()
    return jsonify({"items": items})


@app.route("/api/undo", methods=["POST"])
def api_undo():
    """
    Put an accepted/skipped track back in the review queue so it can be corrected.
    The file keeps whatever tags were written (re-accepting overwrites them), but
    Music.app may have moved it — so re-point the row at its current location.
    """
    fp = (request.get_json(force=True) or {}).get("filepath")
    if not fp:
        return jsonify({"ok": False, "error": "filepath required"}), 400
    c = conn()
    try:
        res = db.undo_action(c, fp)
        if not res:
            return jsonify({"ok": False, "error": "nothing to undo"}), 400
        new_fp = fp
        applied = res.get("applied") or {}
        if not os.path.isfile(fp) and applied.get("title"):
            loc = find_track_location(applied.get("title"), applied.get("artist"))
            if loc and os.path.isfile(loc):
                db.relocate(c, fp, loc)
                new_fp = loc
                log.info("Undo: re-pointed moved file → %s", loc)
    finally:
        c.close()
    log.info("Undid %s: %s", res["prev_status"], os.path.basename(new_fp))
    return jsonify({"ok": True, "filepath": new_fp, "prev_status": res["prev_status"]})


@app.route("/")
def index():
    return render_template(
        "review.html",
        demo=DEMO,
        cover_album_template=cfg.get("cover_album_template", ""),
        original_album_template=cfg.get("original_album_template", "{artist} (Originals)"),
        stamp_cover_grouping=bool(cfg.get("stamp_cover_grouping", True)))


def _enrich(it):
    """Add the raw uploader folder, a 'Topic = likely official' hint, and the four
    scenario options (§1) to an item. Options are recomputed from the stored
    evidence when a row predates option persistence, so old and new rows render
    identically from the one canonical builder in verify.py."""
    parts = (it.get("filepath") or "").split("/")
    raw_folder = parts[-3] if len(parts) >= 3 else ""
    if not it.get("folder_name"):
        it["folder_name"] = raw_folder or None
    artist = ((it.get("proposed") or {}).get("artist") or "")
    it["official_hint"] = "topic" in raw_folder.lower() or "topic" in artist.lower()
    if not it.get("options"):
        try:
            it["options"] = verify.options_from_stored(it)
        except Exception as e:
            log.debug("options rebuild failed for %s: %s", it.get("filepath"), e)
            it["options"] = None


def _paginate(items, offset, limit, enrich=_enrich):
    """Slice a worst-first item list into one page.

    Returns (groups, tier_counts, total). `tier_counts` and `total` span the
    WHOLE list — independent of the slice — so the header pills stay accurate
    across pages and the client knows when to stop. Only the page's items are
    enriched, because the options rebuild is the expensive part and there's no
    point paying it for cards we're not sending.
    """
    total = len(items)
    tier_counts = {v: 0 for v in TIER_ORDER}
    for it in items:
        v = it.get("verdict")
        if v in tier_counts:
            tier_counts[v] += 1
    page = items[offset:offset + limit] if limit else items[offset:]
    for it in page:
        enrich(it)
    groups = {}
    for it in page:
        groups.setdefault(it.get("verdict"), []).append(it)
    payload = [{"verdict": v, "items": groups[v]} for v in TIER_ORDER if groups.get(v)]
    return payload, tier_counts, total


def _int_arg(name, default):
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


@app.route("/api/queue")
def api_queue():
    # Paged so the browser renders ~40 cards at a time, not all 2,731 at once
    # (rendering the whole queue synchronously is what hangs Safari).
    offset = _int_arg("offset", 0)
    limit = _int_arg("limit", 40)   # limit=0 → return everything from offset on
    c = conn()
    # In demo, surface the ALREADY_TAGGED tier as cards too, so all five tiers are
    # visible and the "already tagged — verify & keep" review card is showcased.
    items = db.get_review_queue(c, include_tagged=DEMO)
    counts = db.count_by_status(c)
    c.close()
    # tier_counts always span the full queue so the header pills are correct even
    # while only one page is loaded.
    tier_counts = {v: 0 for v in TIER_ORDER}
    for it in items:
        if it.get("verdict") in tier_counts:
            tier_counts[it["verdict"]] += 1
    # Optional single-tier fetch: "accept all verified" pulls every verified item's
    # data with ?verdict=VERIFIED&limit=0, independent of the current page.
    vf = request.args.get("verdict")
    if vf:
        items = [it for it in items if it.get("verdict") == vf]
    groups, _, total = _paginate(items, offset, limit)
    return jsonify({
        "groups": groups,
        "tier_counts": tier_counts,
        "status_counts": counts,
        "total": total,
        "offset": offset,
        "limit": limit,
    })


# Browser-playable MIME types. macOS mimetypes maps .m4a to "audio/mp4a-latm",
# which browsers refuse to play — so we set these explicitly.
AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "audio/ogg",
}


@app.route("/api/audio")
def api_audio():
    """Stream a local library file — only if it's a known track in the DB."""
    path = request.args.get("path", "")
    c = conn()
    track = db.get_track(c, path)
    c.close()
    if not track or not os.path.isfile(path):
        abort(404)
    ext = os.path.splitext(path)[1].lower()
    return send_file(path, conditional=True, mimetype=AUDIO_MIME.get(ext))


@app.route("/api/art")
def api_art():
    """Serve a file's embedded artwork (e.g. the YouTube thumbnail) or 404."""
    from io import BytesIO
    path = request.args.get("path", "")
    c = conn()
    track = db.get_track(c, path)
    c.close()
    if not track or not os.path.isfile(path):
        abort(404)
    data = mime = None
    try:
        if path.lower().endswith(".mp3"):
            from mutagen.id3 import ID3
            frames = ID3(path).getall("APIC")
            if frames:
                data, mime = frames[0].data, frames[0].mime
        else:
            import mutagen
            mf = mutagen.File(path)
            if mf is not None and mf.tags and "covr" in mf.tags:
                cover = mf.tags["covr"][0]
                data, mime = bytes(cover), "image/png" if cover.imageformat == 14 else "image/jpeg"
    except Exception:
        pass
    if not data:
        abort(404)
    return send_file(BytesIO(data), mimetype=mime or "image/jpeg")


@app.route("/api/preview")
def api_preview():
    """
    On-demand iTunes lookup so a candidate (esp. the AcoustID/MusicBrainz one,
    which has no audio of its own) can be *listened to* before you pick it.
    Returns {preview_url, art_url, album, title, artist} or {} if nothing found.
    """
    if DEMO:
        return jsonify({})   # no network in demo; candidates have no live preview
    artist = request.args.get("artist", "")
    title = request.args.get("title", "")
    hit = itunes.search_track(artist, title)
    return jsonify(hit or {})


def _fetch_preview(url, timeout=10):
    """Download an iTunes preview to a temp .m4a file. Returns the path, or None."""
    try:
        resp = requests.get(url, timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        log.debug("preview download failed (%s): %s", url, e)
        return None
    tf = tempfile.NamedTemporaryFile(prefix="earditor_align_", suffix=".m4a", delete=False)
    tf.write(resp.content)
    tf.close()
    return tf.name


@app.route("/api/align")
def api_align():
    """
    Align the local file at ?path= against the iTunes preview at ?preview= and
    return {ok, offset, confidence, label}. Review-time aid only — nothing is
    stored. Degrades to {ok:false, reason} on any failure so the UI can fall back
    to the manual nudge. Off in demo mode (no real local audio).
    """
    import align
    if DEMO:
        return jsonify({"ok": False, "reason": "unavailable"})
    path = request.args.get("path", "")
    preview = request.args.get("preview", "")
    c = conn()
    track = db.get_track(c, path)
    c.close()
    if not track or not os.path.isfile(path):
        return jsonify({"ok": False, "reason": "unknown_track"})
    if not preview:
        return jsonify({"ok": False, "reason": "no_preview"})
    tmp = _fetch_preview(preview)
    if not tmp:
        return jsonify({"ok": False, "reason": "download_failed"})
    try:
        res = align.align_audio(path, tmp)
    except ImportError as e:
        log.warning("align unavailable (librosa/ffmpeg missing): %s", e)
        return jsonify({"ok": False, "reason": "unavailable"})
    except Exception as e:
        log.debug("align decode failed for %s: %s", path, e)
        return jsonify({"ok": False, "reason": "decode_failed"})
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return jsonify({
        "ok": True,
        "offset": res["offset_sec"],
        "confidence": res["confidence"],
        "label": align.confidence_label(res["confidence"]),
    })


def _apply(fp, tags):
    # Demo mode writes NOTHING to disk or Music.app — it just advances the queue so
    # a stranger can click through the whole flow safely. No tags, no AppleScript.
    if DEMO:
        c = conn()
        db.mark_accepted(c, fp, tags)
        c.close()
        return True
    ok = apply_tags(fp, tags, cfg.playlist_name)
    if ok:
        c = conn()
        db.mark_accepted(c, fp, tags)
        c.close()
    return ok


@app.route("/api/accept", methods=["POST"])
def api_accept():
    data = request.get_json(force=True)
    fp, tags = data.get("filepath"), data.get("tags") or {}
    if not fp or not tags.get("title"):
        return jsonify({"ok": False, "error": "missing filepath or title"}), 400
    ok = _apply(fp, tags)
    return jsonify({"ok": ok, "demo": DEMO})


@app.route("/api/batch_accept", methods=["POST"])
def api_batch_accept():
    data = request.get_json(force=True)
    results = {}
    for entry in data.get("tracks", []):
        fp, tags = entry.get("filepath"), entry.get("tags") or {}
        if fp and tags.get("title"):
            results[fp] = _apply(fp, tags)
    accepted = sum(1 for v in results.values() if v)
    return jsonify({"ok": True, "accepted": accepted, "results": results})


@app.route("/api/skip", methods=["POST"])
def api_skip():
    data = request.get_json(force=True)
    fp = data.get("filepath")
    if not fp:
        return jsonify({"ok": False}), 400
    c = conn()
    db.mark_skipped(c, fp)
    c.close()
    return jsonify({"ok": True})


def _free_port(preferred):
    """Return `preferred` if bindable, otherwise an OS-assigned free port."""
    for candidate in (preferred, 0):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", candidate))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            continue
    return preferred


# ---------------------------------------------------------------------------
# Demo mode — load the synthetic fixtures into a throwaway demo.db so a stranger
# gets the full review experience with no config, no keys, and no real files.
# ---------------------------------------------------------------------------
def _reset_demo_db():
    """Start each demo run from a clean throwaway DB (never the real earditor.db)."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DEMO_DB_PATH) + suffix)
        if p.exists():
            p.unlink()


def load_demo_fixtures():
    """Insert the synthetic fixture rows into demo.db via the normal db layer.
    Returns the number of rows loaded."""
    data = json.loads(DEMO_FIXTURES.read_text(encoding="utf-8"))
    rows = data.get("rows", [])
    c = conn()
    try:
        for row in rows:
            fp = row["filepath"]
            db.add_pending(c, fp)
            if row.get("tagged"):
                db.mark_tagged(c, fp, row["tagged"])
            else:
                db.save_scan_result(c, fp, row)
    finally:
        c.close()
    return len(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Earditor review UI")
    ap.add_argument("--demo", action="store_true",
                    help="Load synthetic fixtures into a throwaway demo.db — no config, "
                         "no API keys, no real files, no tag writes, no Music.app. "
                         "Every visible string is fictional.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5001")),
                    help="Preferred port (falls back to a free one if busy).")
    args = ap.parse_args()

    if args.demo:
        if not DEMO_FIXTURES.exists():
            log.error("Demo fixtures missing at %s — run: python3 demo/build_fixtures.py",
                      DEMO_FIXTURES)
            sys.exit(1)
        DEMO = True
        DB_PATH = DEMO_DB_PATH        # route the whole app at the throwaway DB
        _reset_demo_db()
        db.init_db(str(DB_PATH))
        n = load_demo_fixtures()
        log.info("★ DEMO MODE — %d synthetic tracks loaded. No real files or Music.app "
                 "are touched; accepting writes nothing.", n)
    else:
        db.init_db(str(DB_PATH))

    port = _free_port(args.port)
    if port != args.port:
        log.warning("Port %d busy — using free port %d instead", args.port, port)
    log.info("Review UI → http://127.0.0.1:%d  (db=%s)", port, DB_PATH)
    app.run(host="127.0.0.1", port=port, debug=False)

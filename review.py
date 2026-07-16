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

import config
import db
import scan
import verify
from config import load_config, DB_PATH, DATA_DIR, __version__
from tagger import apply_tags
from itunes_bridge import find_track_location
from sources.itunes import iTunesSource

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("earditor.review")

app = Flask(__name__)
cfg = load_config()
itunes = iTunesSource(delay=0)

HERE = Path(__file__).resolve().parent
# Resolves $RESOURCEPATH (py2app), sys._MEIPASS (PyInstaller), or the source dir.
RESOURCE_ROOT = config.resource_root()
app.template_folder = str(RESOURCE_ROOT / "templates")

# Demo mode (python3 review.py --demo): loads synthetic fixtures into a throwaway
# demo.db, writes nothing to real files or Music.app, and needs no config or keys.
# Set in __main__ before the server starts, then read as a global by the routes.
DEMO = False
# DATA_DIR, not HERE: in a packaged app HERE is inside the read-only bundle. For a
# source checkout the two are the same path, so this changes nothing there.
DEMO_DB_PATH = DATA_DIR / "demo.db"
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
# Background jobs — the two long-running things the UI can start without a
# terminal. One job at a time, guarded by _scan_lock:
#
#   "triage" — the 60-second audit. Walks the library, registers what's there,
#              and retires every already-clean file. Tag reads only, so it runs
#              in-process and reports true file-by-file progress.
#   "scan"   — identification. Runs scan.py (subprocess from source, nested
#              thread when frozen), and progress is polled out of the DB.
#
# Progress is counted in FILES, not review cards: most files resolve without
# ever producing a card, so a card counter reads as frozen mid-run.
# ---------------------------------------------------------------------------
_scan_lock = threading.Lock()
JOB = {
    "job": None,          # "scan" | "triage" | None
    "running": False,
    "started_at": None,
    "finished_at": None,
    "files_done": 0,      # files this run has chewed through
    "files_total": 0,     # pending backlog at run start (post-filter)
    "cards_ready": 0,     # new review cards this run has produced
    "returncode": None,
    "error": None,
}


def _status_counts():
    c = conn()
    try:
        return db.count_by_status(c)
    finally:
        c.close()


def _finish_job(returncode, error=None):
    with _scan_lock:
        JOB.update(running=False, returncode=returncode, finished_at=time.time())
        if error:
            JOB["error"] = error
        elif returncode not in (0, None):
            JOB["error"] = f"scan exited with code {returncode}"


def _pending_total(cfg_local):
    """Post-filter pending count — the denominator for "file 214 of 5,260"."""
    c = conn()
    try:
        return len(db.get_pending(c, predicate=cfg_local.allows))
    finally:
        c.close()


def _triage_worker():
    """The 60-second audit: register every file under music_path, then retire the
    ones whose tags are already clean. No network, no writes to any audio file."""
    cfg_local = load_config()
    music_path = cfg_local.music_path
    if not os.path.isdir(music_path):
        _finish_job(None, f"Music folder not found: {music_path}")
        return
    c = conn()
    try:
        files = list(scan.walk_library(
            music_path, cfg_local.audio_extensions,
            cfg_local.include_paths, cfg_local.exclude_paths,
        ))
        db.add_pending_bulk(c, files)

        def progress(done, total):
            with _scan_lock:
                JOB["files_done"] = done
                JOB["files_total"] = total

        scan.triage(c, log, predicate=cfg_local.allows, on_progress=progress)
    except Exception as e:
        log.exception("Triage failed: %s", e)
        _finish_job(None, str(e))
        return
    finally:
        c.close()
    _finish_job(0)
    log.info("Triage finished")


def _scan_worker():
    """
    Run scan.py until it produces SCAN_QUEUE_TARGET review cards, publishing
    file-level progress the whole way.
    """
    def _resolved(counts):
        # Files that have left 'pending'. Robust to the library walk ADDING pending
        # rows mid-run (moved/new files), which made `start_pending - pending` go 0.
        return sum(v for k, v in counts.items() if k != "pending")

    before = _status_counts()
    start_cards = before.get("scanned", 0)
    start_resolved = _resolved(before)
    # Counted BEFORE taking the lock: it walks every pending row, and holding the
    # lock across that would stall each /api/scan/status poll behind it.
    total = _pending_total(load_config())
    with _scan_lock:
        JOB.update(files_done=0, cards_ready=0, files_total=total)
    scan_args = ["--queue-target", str(SCAN_QUEUE_TARGET),
                 "--max-files", str(SCAN_MAX_FILES)]
    try:
        if getattr(sys, "frozen", False):
            # A py2app bundle has no standalone scan.py to launch. Reuse the same
            # CLI entry point in a nested worker so this thread can keep publishing
            # live progress while the native Scan button does its work.
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
    except Exception as e:
        _finish_job(None, str(e))
        log.exception("Failed to launch scan subprocess: %s", e)
        return

    while ((proc is not None and proc.poll() is None) or
           (proc is None and scan_thread.is_alive())):
        counts = _status_counts()
        with _scan_lock:
            JOB["cards_ready"] = max(0, counts.get("scanned", 0) - start_cards)
            JOB["files_done"] = max(0, _resolved(counts) - start_resolved)
        time.sleep(1.0)
    returncode = proc.returncode if proc is not None else result["returncode"]

    # Final read AFTER the process exits: the last poll fires up to a second
    # before the run ends, which otherwise leaves the bar stranded at 94%.
    counts = _status_counts()
    with _scan_lock:
        JOB["cards_ready"] = max(0, counts.get("scanned", 0) - start_cards)
        JOB["files_done"] = max(0, _resolved(counts) - start_resolved)
    _finish_job(returncode)
    log.info("Background scan finished (returncode=%s, new cards=%s)",
             returncode, JOB["cards_ready"])


def _job_snapshot():
    """Header counts + job progress, cheap enough to poll every ~1.5s.

    Deliberately excludes the path-filter skip counts: those walk every pending
    row in Python, which is fine for /api/stats on a page load but not here.
    """
    c = conn()
    try:
        status_counts = db.count_by_status(c)
        tier_counts = db.count_by_verdict(c)
    finally:
        c.close()
    with _scan_lock:
        s = dict(JOB)
    # Rolling average over the whole run — the honest "~6s per file", which for
    # identification is dominated by the politeness delays, not by the machine.
    end = s["finished_at"] or time.time()
    elapsed = (end - s["started_at"]) if s["started_at"] else 0
    secs_per_file = round(elapsed / s["files_done"], 1) if s["files_done"] else None
    return {
        "job": s["job"],
        "running": s["running"],
        "files_done": s["files_done"],
        "files_total": s["files_total"],
        "cards_ready": s["cards_ready"],
        "secs_per_file": secs_per_file,
        "pending": status_counts.get("pending", 0),   # raw file backlog, NOT cards
        "error": s["error"],
        "tier_counts": tier_counts,
        "status_counts": status_counts,
    }


JOB_WORKERS = {"scan": _scan_worker, "triage": _triage_worker}


@app.route("/api/scan/start", methods=["POST"])
def api_scan_start():
    if DEMO:
        return jsonify({"ok": False, "error": "demo"}), 400
    job = (request.get_json(silent=True) or {}).get("job", "scan")
    if job not in JOB_WORKERS:
        return jsonify({"ok": False, "error": "unknown_job"}), 400
    with _scan_lock:
        if JOB["running"]:
            return jsonify({"ok": False, "error": "already_running"}), 409
        JOB.update(job=job, running=True, started_at=time.time(), finished_at=None,
                   files_done=0, files_total=0, cards_ready=0,
                   returncode=None, error=None)
    threading.Thread(target=JOB_WORKERS[job], daemon=True).start()
    log.info("Background %s started", job)
    return jsonify({"ok": True, "job": job})


@app.route("/api/scan/status")
def api_scan_status():
    return jsonify(_job_snapshot())


@app.route("/api/stats")
def api_stats():
    """Library-shape numbers for the report card: how much is already clean, how
    much is left, and what the path filters are holding back."""
    c = conn()
    try:
        status_counts = db.count_by_status(c)
        filters = scan.count_skipped_pending(c, cfg.music_path, cfg)
    finally:
        c.close()
    total = sum(status_counts.values())
    # "Clean" is what needs nothing from you: retired by triage, or already
    # accepted in review. Everything else is still work.
    clean = status_counts.get("tagged", 0) + status_counts.get("accepted", 0)
    return jsonify({
        "total": total,
        "clean": clean,
        "pending": status_counts.get("pending", 0),
        "status_counts": status_counts,
        "filters": filters,
        "music_path": cfg.music_path,
    })


# Injected by packaging/app.py when there's a native window to hang a dialog off.
# review.py must never import webview: a source install has no window, and the
# import alone would make the browser path depend on a GUI toolkit. Returns the
# chosen directory, or None if the user cancelled.
FOLDER_PICKER = None
CONFIG_EXAMPLE = RESOURCE_ROOT / "config.example.json"

# The only keys /api/config will write. Everything else stays a hand-edit, so a
# UI bug can't rewrite thresholds or delays behind the user's back.
EDITABLE_KEYS = ("music_path",)


def _write_config(updates):
    """Merge `updates` into config.json in DATA_DIR and reload the live config.

    Seeds from config.example.json when there's no config yet, so a first-run
    write leaves the user a fully commented file rather than a one-key stub.
    """
    global cfg
    path = Path(config.CONFIG_PATH)
    data = {}
    for src in (path, CONFIG_EXAMPLE):
        if src.exists():
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
                break
            except Exception as e:
                log.warning("Could not parse %s (%s) — starting from defaults", src, e)
    data.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: a crash mid-write must not leave a truncated config.json
    # that the next launch would fall back to defaults for.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    # Reload from the same path we just wrote — load_config()'s default argument
    # binds at import, so it would not follow a relocated CONFIG_PATH.
    cfg = load_config(path)      # the running process must not hold a stale path
    log.info("Config updated (%s) → %s", ", ".join(updates), path)
    return cfg


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify({
            "music_path": cfg.get("music_path"),
            "music_path_resolved": cfg.music_path,
            "music_path_exists": os.path.isdir(cfg.music_path),
            "can_pick_folder": FOLDER_PICKER is not None,
        })
    if DEMO:
        return jsonify({"ok": False, "error": "demo"}), 400
    body = request.get_json(silent=True) or {}
    updates = {k: body[k] for k in EDITABLE_KEYS if k in body}
    if not updates:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    raw = str(updates.get("music_path", "")).strip()
    # Validate what the path MEANS (expanded), but persist what the user typed:
    # "~/Music" stays portable across machines, an absolute path stays exact.
    resolved = os.path.expanduser(os.path.expandvars(raw))
    if not resolved or not os.path.isdir(resolved):
        return jsonify({"ok": False, "error": "not_a_directory", "path": resolved}), 400
    updates["music_path"] = raw
    new_cfg = _write_config(updates)
    return jsonify({"ok": True, "music_path": new_cfg.get("music_path"),
                    "music_path_resolved": new_cfg.music_path})


@app.route("/api/config/pick_folder", methods=["POST"])
def api_pick_folder():
    """Open the OS folder picker — packaged app only (needs a native window)."""
    if FOLDER_PICKER is None:
        return jsonify({"ok": False, "error": "no_window"}), 400
    try:
        chosen = FOLDER_PICKER()
    except Exception as e:
        log.exception("Folder picker failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    if not chosen:
        return jsonify({"ok": False, "error": "cancelled"})
    return jsonify({"ok": True, "path": chosen})


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
        # Only Music.app relocates files behind our back, so the re-point lookup is
        # only meaningful when it's in play. Without it the stored path IS the path.
        if (cfg.music_app_integration and not os.path.isfile(fp)
                and applied.get("title")):
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
        version=__version__,
        cover_album_template=cfg.get("cover_album_template", ""),
        original_album_template=cfg.get("original_album_template", "{artist} (Originals)"),
        stamp_cover_grouping=bool(cfg.get("stamp_cover_grouping", True)))


def _enrich(it):
    """Add the raw uploader folder, a 'Topic = likely official' hint, and the four
    scenario options (§1) to an item. Options are recomputed from the stored
    evidence when a row predates option persistence, so old and new rows render
    identically from the one canonical builder in verify.py."""
    # Normalize separators before splitting, as utils.raw_folder_name does: a
    # Windows path has no "/" to split on, which would silently blank the uploader
    # folder — and the folder IS the uploader identity behind S6 and the Topic hint.
    parts = (it.get("filepath") or "").replace("\\", "/").split("/")
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
    if DEMO:
        return jsonify({"ok": False, "reason": "unavailable"})
    try:
        import align
    except ImportError as e:
        log.warning("align unavailable (numpy/librosa/ffmpeg missing): %s", e)
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
    # music_app tells the UI which toast to show: a plain "tagged" reads as a
    # failure to anyone who expects the track to appear in their playlist.
    return jsonify({"ok": ok, "demo": DEMO, "music_app": cfg.music_app_integration})


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


def enable_demo():
    """Point the whole app at a throwaway demo DB and load the fixtures.

    Lives here rather than in __main__ so the packaged app (packaging/app.py) can
    reach it too — `--demo` is the support answer for "is it broken or is it my
    setup?", which means it has to work identically in the shipped app.

    Returns the number of synthetic tracks loaded. Raises FileNotFoundError when
    the fixtures weren't bundled.
    """
    global DEMO, DB_PATH
    if not DEMO_FIXTURES.exists():
        raise FileNotFoundError(DEMO_FIXTURES)
    DEMO = True
    DB_PATH = DEMO_DB_PATH        # route the whole app at the throwaway DB
    _reset_demo_db()
    db.init_db(str(DB_PATH))
    return load_demo_fixtures()


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
        try:
            n = enable_demo()
        except FileNotFoundError:
            log.error("Demo fixtures missing at %s — run: python3 demo/build_fixtures.py",
                      DEMO_FIXTURES)
            sys.exit(1)
        log.info("★ DEMO MODE — %d synthetic tracks loaded. No real files or Music.app "
                 "are touched; accepting writes nothing.", n)
    else:
        db.init_db(str(DB_PATH))

    port = _free_port(args.port)
    if port != args.port:
        log.warning("Port %d busy — using free port %d instead", args.port, port)
    log.info("Review UI → http://127.0.0.1:%d  (db=%s)", port, DB_PATH)
    app.run(host="127.0.0.1", port=port, debug=False)

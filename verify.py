#!/usr/bin/env python3
"""
verify.py — the heart of Earditor: deterministic cross-verification.

Given evidence gathered independently from Shazam, AcoustID/MusicBrainz, iTunes,
and the file itself, compute six signals (S1-S6), assign a verdict tier, and
propose tags ONLY when the evidence supports them. No LLM. No guessing.

Everything here is a pure function of its inputs, so tests/test_verify.py can
drive it with hand-built fixtures.

Signals
  S1 fingerprint agreement   AcoustID vs Shazam (title+artist)   — anti "wrong song"
  S2 catalog confirmation    iTunes has this artist+title
  S3 duration sanity         file duration vs candidate duration
  S4 filename corroboration  filename tokens vs title AND artist (romaji-aware)
  S5 cover/remix indicators  deterministic keyword scan (covers.py)
  S6 folder-artist relation  folder == artist / neutral channel / different artist

Verdict tiers
  VERIFIED    S1 & S3 pass, no S5 hits — OR the Topic fast-pass (below)
  LIKELY      S2 & S3 & (S4 or S6-match), but no fingerprint agreement
  COVER       S5 hit, or (S6 different & S4 title-match but artist-mismatch)
  UNVERIFIED  something came back but signals conflict / nothing corroborates
  NO_MATCH    no fingerprint anywhere

Topic-channel fast-pass (evaluated before the normal tree)
  "Artist - Topic" folders are YouTube Music's auto-generated official artist
  channels, built from label metadata — the channel artist is a reliable identity
  and these are NEVER covers. So when the folder is a Topic channel:
    - candidate artist matches the channel name (minus "- Topic") → VERIFIED and
      auto_accept=True (scan.py applies tags immediately; never enters the queue).
    - artist doesn't match, or nothing came back from the APIs → UNVERIFIED
      (manual review). We never auto-accept on the folder name alone.
"""

import os
import unicodedata

from rapidfuzz import fuzz

from utils import normalize, to_romaji, has_japanese, title_from_filename
from covers import detect_cover_signals, is_neutral_channel, topic_channel_artist

DEFAULT_THRESHOLDS = {
    "fuzzy_match": 85,
    "duration_pct": 0.10,
    "duration_abs_sec": 15,
    "acoustid_min_score": 0.5,
}

# Verdicts
VERIFIED = "VERIFIED"
LIKELY = "LIKELY"
COVER = "COVER"
UNVERIFIED = "UNVERIFIED"
NO_MATCH = "NO_MATCH"


# --- fuzzy helpers -------------------------------------------------------------
def _both_forms(text):
    """Return (normalized, romaji) forms so JP and romaji both compare."""
    if not text:
        return "", ""
    norm = normalize(text)
    rom = to_romaji(text) if has_japanese(text) else norm
    return norm, rom


def ratio(a, b):
    """Best token_set_ratio between two strings, romaji-aware. 0-100."""
    if not a or not b:
        return 0.0
    an, ar = _both_forms(a)
    bn, br = _both_forms(b)
    return max(
        fuzz.token_set_ratio(an, bn),
        fuzz.token_set_ratio(ar, br),
    )


def contains_ratio(haystack, needle):
    """
    How well `needle` (a title/artist) is corroborated *within* `haystack`
    (a noisy filename). Uses partial + token_set, romaji-aware. 0-100.
    """
    if not haystack or not needle:
        return 0.0
    hn, hr = _both_forms(haystack)
    nn, nr = _both_forms(needle)
    return max(
        fuzz.token_set_ratio(hn, nn), fuzz.partial_ratio(nn, hn),
        fuzz.token_set_ratio(hr, nr), fuzz.partial_ratio(nr, hr),
    )


def _sig(sid, label, status, passed, value, explain):
    return {"id": sid, "label": label, "status": status,
            "pass": passed, "value": value, "explain": explain}


def _validate_folder_release(hit, folder, primary_title, T):
    """
    An iTunes hit only counts as "the folder artist's own release of this title" if
    BOTH its artist really is the folder artist and its title really is this song.
    Otherwise iTunes' fuzzy search would hijack the proposal with an unrelated track.
    """
    if not hit or not folder or not primary_title:
        return None
    if ratio(hit.get("artist"), folder) < T:
        return None
    if ratio(hit.get("title"), primary_title) < T:
        return None
    return hit


# --- individual signals --------------------------------------------------------
def signal_s1(shazam, acoustid, T):
    """Fingerprint agreement: does AcoustID corroborate Shazam?"""
    if not shazam:
        return _sig("S1", "Fingerprint agreement", "neutral", False, None,
                    "No Shazam result to cross-check")
    if not acoustid:
        return _sig("S1", "Fingerprint agreement", "neutral", False, None,
                    "No independent fingerprint (AcoustID had no match)")

    tr = ratio(shazam.get("title"), acoustid.get("title"))
    ar = ratio(shazam.get("artist"), acoustid.get("artist")) if (
        shazam.get("artist") and acoustid.get("artist")) else None

    ac_desc = f"{acoustid.get('title')} — {acoustid.get('artist')}"
    if tr >= T and (ar is None or ar >= 70):
        return _sig("S1", "Fingerprint agreement", "green", True,
                    {"title": round(tr), "artist": round(ar) if ar is not None else None},
                    "AcoustID agrees with Shazam")
    return _sig("S1", "Fingerprint agreement", "red", False,
                {"title": round(tr), "artist": round(ar) if ar is not None else None},
                f"AcoustID disagrees — it heard “{ac_desc}”")


def signal_s2(primary_title, primary_artist, itunes, T):
    """Catalog confirmation: does iTunes list this artist+title?"""
    if not itunes:
        return _sig("S2", "Catalog confirmation", "red", False, None,
                    "Not found in the iTunes catalog")
    tr = ratio(primary_title, itunes.get("title"))
    ar = ratio(primary_artist, itunes.get("artist")) if (
        primary_artist and itunes.get("artist")) else None
    if tr >= T and (ar is None or ar >= T):
        return _sig("S2", "Catalog confirmation", "green", True,
                    {"title": round(tr), "artist": round(ar) if ar is not None else None},
                    f"Confirmed on iTunes: “{itunes.get('album') or itunes.get('title')}”")
    return _sig("S2", "Catalog confirmation", "yellow", False,
                {"title": round(tr), "artist": round(ar) if ar is not None else None},
                f"iTunes returned a different track (“{itunes.get('title')}”)")


def _candidate_duration(itunes, acoustid):
    if itunes and itunes.get("duration"):
        return itunes["duration"], "iTunes"
    if acoustid and acoustid.get("duration"):
        return acoustid["duration"], "AcoustID"
    return None, None


def signal_s3(file_duration, itunes, acoustid, T_pct, T_abs):
    """Duration sanity: file length vs candidate length (YouTube rips have padding)."""
    cand, src = _candidate_duration(itunes, acoustid)
    if not file_duration or not cand:
        return _sig("S3", "Duration sanity", "neutral", False, None,
                    "Duration unknown — can't compare")
    delta = abs(file_duration - cand)
    tolerance = max(cand * T_pct, T_abs)
    if delta <= tolerance:
        return _sig("S3", "Duration sanity", "green", True, round(delta, 1),
                    f"Length matches {src} (±{round(delta)}s)")
    return _sig("S3", "Duration sanity", "red", False, round(delta, 1),
                f"Length off by {round(delta)}s vs {src}")


def signal_s4(tokens, primary_title, primary_artist, T):
    """Filename corroboration: does the filename mention this title AND artist?"""
    filename = tokens.get("raw") if isinstance(tokens, dict) else str(tokens)
    tm = contains_ratio(filename, primary_title) if primary_title else 0.0
    am = contains_ratio(filename, primary_artist) if primary_artist else 0.0
    value = {"title": round(tm), "artist": round(am)}
    if tm >= T and am >= T:
        return _sig("S4", "Filename corroboration", "green", True, value,
                    "Filename matches both title and artist")
    if tm >= T:
        return _sig("S4", "Filename corroboration", "yellow", True, value,
                    "Filename matches the title but not the artist")
    if am >= T:
        return _sig("S4", "Filename corroboration", "yellow", False, value,
                    "Filename matches the artist but not the title")
    return _sig("S4", "Filename corroboration", "red", False, value,
                "Filename doesn't corroborate the match")


def signal_s5(filename_raw, folder_name):
    """Cover/remix indicators from filename + folder."""
    text = unicodedata.normalize("NFKC", f"{filename_raw or ''} {folder_name or ''}")
    hits = detect_cover_signals(text)
    if hits:
        terms = ", ".join(h["term"] for h in hits[:4])
        return _sig("S5", "Cover / remix indicators", "yellow", True,
                    [h["term"] for h in hits],
                    f"Cover/remix keywords found: {terms}")
    return _sig("S5", "Cover / remix indicators", "green", False, [],
                "No cover/remix keywords")


def signal_s6(folder_name, primary_artist, T):
    """Folder-artist relationship: match / neutral / different."""
    if not folder_name:
        return _sig("S6", "Folder vs artist", "neutral", False,
                    {"relationship": "unknown"}, "No artist folder in path")
    if is_neutral_channel(folder_name):
        return _sig("S6", "Folder vs artist", "neutral", False,
                    {"relationship": "neutral", "folder": folder_name},
                    f"Folder “{folder_name}” is a label/auto channel — neutral")
    r = ratio(folder_name, primary_artist) if primary_artist else 0.0
    if r >= T:
        return _sig("S6", "Folder vs artist", "green", True,
                    {"relationship": "match", "folder": folder_name, "score": round(r)},
                    f"Folder “{folder_name}” matches the artist")
    return _sig("S6", "Folder vs artist", "yellow", False,
                {"relationship": "different", "folder": folder_name, "score": round(r)},
                f"Folder “{folder_name}” is a different artist — possible cover")


# --- orchestration -------------------------------------------------------------
def verify(evidence, thresholds=None):
    """
    Run all signals and assign a verdict. `evidence` keys:
      file_duration, folder_name, filename (raw path/name), tokens (optional dict),
      shazam, acoustid, itunes, itunes_candidates
    Returns dict: {verdict, signals{S1..S6}, proposed, candidates, score}.
    """
    T = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        T.update(thresholds)
    fuzzy = T["fuzzy_match"]

    shazam = evidence.get("shazam")
    acoustid = evidence.get("acoustid")
    itunes = evidence.get("itunes")
    folder = evidence.get("folder_name")
    filename_raw = evidence.get("filename") or ""
    tokens = evidence.get("tokens") or {"raw": filename_raw}
    # S5 scans the ORIGINAL filename (brackets intact) so it sees 【歌ってみた】 etc.
    filename_for_covers = (evidence.get("filename_original")
                           or (tokens.get("original") if isinstance(tokens, dict) else None)
                           or filename_raw)
    file_dur = evidence.get("file_duration")

    # Primary candidate: prefer Shazam, fall back to AcoustID.
    primary_title = (shazam or {}).get("title") or (acoustid or {}).get("title")
    primary_artist = (shazam or {}).get("artist") or (acoustid or {}).get("artist")

    # The folder artist's OWN catalog release of this title, if there is one.
    # Cover songs are the hard case: several artists release the same title, and a
    # fingerprint can confidently name the wrong one (AcoustID/MusicBrainz is
    # crowd-sourced and sometimes mis-credited). The folder says whose upload this
    # is, so if that artist has a real release of this title, that's strong evidence.
    folder_release = _validate_folder_release(
        evidence.get("itunes_folder"), folder, primary_title, fuzzy)
    # A conflict = the folder artist has this title, but the fingerprint named
    # someone else. Genuine ambiguity — never auto-verify it.
    folder_conflict = bool(
        folder_release and primary_artist
        and ratio(folder_release.get("artist"), primary_artist) < fuzzy)

    # iTunes only counts as evidence if it actually matches the identified track.
    # A wrong iTunes hit must NOT poison duration (S3) or the proposed album/art —
    # it's kept only as a review candidate. (This is the "Alcedo Atthis" bug.)
    itunes_ok = False
    if itunes:
        it_tr = ratio(primary_title, itunes.get("title"))
        it_ar = (ratio(primary_artist, itunes.get("artist"))
                 if primary_artist and itunes.get("artist") else None)
        itunes_ok = it_tr >= fuzzy and (it_ar is None or it_ar >= 70)
    itunes_trusted = itunes if itunes_ok else None

    signals = {
        "S1": signal_s1(shazam, acoustid, fuzzy),
        "S2": signal_s2(primary_title, primary_artist, itunes, fuzzy),
        "S3": signal_s3(file_dur, itunes_trusted, acoustid, T["duration_pct"], T["duration_abs_sec"]),
        "S4": signal_s4(tokens, primary_title, primary_artist, fuzzy),
        "S5": signal_s5(filename_for_covers, folder),
        "S6": signal_s6(folder, primary_artist, fuzzy),
    }

    # Topic-channel fast-pass runs BEFORE the normal decision tree. folder_name
    # arrives already stripped of "- Topic" in production, so detect from the raw
    # folder first (falling back to folder_name for callers that don't split them).
    folder_raw = evidence.get("folder_raw")
    topic_artist = topic_channel_artist(folder_raw) or topic_channel_artist(folder)

    auto_accept = False
    if topic_artist:
        # "Artist - Topic" is authoritative. Auto-accept only when a real candidate
        # (Shazam/AcoustID) artist matches the channel — never on the folder alone.
        if primary_title and primary_artist and ratio(topic_artist, primary_artist) >= fuzzy:
            verdict = VERIFIED
            auto_accept = True
        else:
            verdict = UNVERIFIED
    else:
        verdict = _decide(signals, shazam, acoustid, fuzzy)

    # The folder artist has their own release of this title, but the fingerprint
    # named a different artist. Two real recordings of the same song — the machine
    # can't settle it, so never let this reach VERIFIED (batch-acceptable). Propose
    # the folder artist's release and show both readings side by side.
    if folder_conflict and verdict != NO_MATCH:
        verdict = LIKELY

    proposed, candidates = _propose(
        verdict, signals, evidence, itunes_trusted, primary_title, primary_artist,
        folder_release=folder_release if folder_conflict else None)
    options = _build_options(
        evidence, signals, itunes_trusted, primary_title, primary_artist,
        verdict, filename_for_covers, fuzzy)
    score = _score(signals, verdict)

    return {"verdict": verdict, "signals": signals, "auto_accept": auto_accept,
            "proposed": proposed, "candidates": candidates, "options": options,
            "score": score}


def options_from_stored(item):
    """
    Rebuild the four review options (§1) from a stored DB row, so rows scanned
    before options were persisted still get identical option data from the one
    canonical builder (_build_options). Returns None if the row has no signals.
    `item` keys used: shazam, acoustid, itunes, folder_name, filepath, verdict, signals.
    """
    signals = item.get("signals")
    if not signals:
        return None
    shazam = item.get("shazam")
    acoustid = item.get("acoustid")
    itunes = item.get("itunes")
    folder = item.get("folder_name")
    filename_original = os.path.basename(item.get("filepath") or "")
    T = DEFAULT_THRESHOLDS["fuzzy_match"]

    primary_title = (shazam or {}).get("title") or (acoustid or {}).get("title")
    primary_artist = (shazam or {}).get("artist") or (acoustid or {}).get("artist")
    # Recompute which iTunes hit is trustworthy (same rule as verify()).
    itunes_ok = False
    if itunes:
        it_tr = ratio(primary_title, itunes.get("title"))
        it_ar = (ratio(primary_artist, itunes.get("artist"))
                 if primary_artist and itunes.get("artist") else None)
        itunes_ok = it_tr >= T and (it_ar is None or it_ar >= 70)
    itunes_trusted = itunes if itunes_ok else None

    evidence = {"shazam": shazam, "acoustid": acoustid, "itunes": itunes,
                "folder_name": folder}
    return _build_options(evidence, signals, itunes_trusted, primary_title,
                          primary_artist, item.get("verdict"), filename_original, T)


def _decide(signals, shazam, acoustid, T):
    if not shazam and not acoustid:
        return NO_MATCH

    s5_hit = signals["S5"]["pass"]
    s4 = signals["S4"]["value"] or {}
    s6_rel = (signals["S6"]["value"] or {}).get("relationship")
    # Folder-vs-artist as a cover signal is WEAK: a differing folder usually just
    # means the folder is a YouTube *uploader* channel (a re-upload of the original),
    # not a cover artist. So only treat it as a cover when:
    #   - there's a real Shazam fingerprint (not AcoustID-only OST compilations), AND
    #   - the catalog does NOT confirm the original artist (S2 fail) — because if
    #     iTunes/Shazam confirm the real artist, it's a re-upload, keep that artist.
    # Genuine covers almost always also carry an S5 keyword (歌ってみた, "cover by"),
    # which is the primary COVER trigger below.
    # 6a: if an independent fingerprint already agrees on the artist (S1 pass), a
    # differing folder is just a re-uploader channel, never a cover — even when the
    # song isn't in iTunes (S2 fail, common for older/JP-only anime). Only infer a
    # cover from the folder when the fingerprint ISN'T independently corroborated.
    cover_by_folder = (
        shazam is not None
        and not signals["S1"]["pass"]
        and s6_rel == "different"
        and s4.get("title", 0) >= T
        and s4.get("artist", 0) < T
        and not signals["S2"]["pass"]
    )

    # A COVER still keeps the identified *title* (only the artist is reassigned to
    # the uploader), so the title must be trustworthy. It is trustworthy only if an
    # independent fingerprint agrees (S1) OR the filename actually contains it (S4).
    # Without that, a cover keyword on a WRONG-SONG fingerprint would produce a
    # bogus COVER card — so fall through to UNVERIFIED and show candidates instead.
    ident_trustworthy = signals["S1"]["pass"] or s4.get("title", 0) >= T

    # A strong lone AcoustID match (Shazam absent) is independent evidence in its own
    # right — AcoustID scores ~0.99 on true fingerprint matches. Trust it when the
    # duration matches AND the filename/folder corroborate it. This rescues definitive
    # IDs (e.g. instrumental OST tracks Shazam can't hear) from needless UNVERIFIED.
    strong_acoustid = bool(
        acoustid and not shazam and acoustid.get("score", 0) >= 0.90)
    corroborated = s4.get("title", 0) >= T or s6_rel == "match"

    # A COVER exists to fix a WRONG artist: it reassigns the artist to the uploader
    # and blanks the album. But if the catalog confirms this exact artist+title (S2)
    # AND the folder agrees it's that artist (S6 match), the artist isn't wrong —
    # it's an official release that merely has a version keyword in its title
    # ("[feat. X]", "(Acoustic Ver.)"). Keep the catalog's album + art rather than
    # throwing them away. (A real cover has a folder that differs from the catalog
    # artist, so this never rescues one — see cover_by_folder above.)
    catalog_confirms_artist = signals["S2"]["pass"] and s6_rel == "match"

    if (s5_hit or cover_by_folder) and ident_trustworthy and not catalog_confirms_artist:
        return COVER
    if signals["S1"]["pass"] and signals["S3"]["pass"]:
        return VERIFIED
    if strong_acoustid and signals["S3"]["pass"] and corroborated:
        return VERIFIED
    # 6b: iTunes (S2) is queried with the fingerprint's own title/artist, so it
    # echoes a wrong-song fingerprint back to itself — S2 is not independent
    # evidence the title is right. Require real title corroboration (S4-title;
    # S1 would already have VERIFIED above) before S2 can reach LIKELY. A folder
    # that matches the artist (S6 match) corroborates only the artist, not the
    # title, so it is NOT enough on its own to lift an echoed title.
    if signals["S2"]["pass"] and signals["S3"]["pass"] and signals["S4"]["pass"]:
        return LIKELY
    if strong_acoustid and corroborated:
        # fingerprint + filename/folder agree, but duration unknown/off → review individually
        return LIKELY
    return UNVERIFIED


def _propose(verdict, signals, evidence, itunes_trusted, primary_title, primary_artist,
             folder_release=None):
    shazam = evidence.get("shazam") or {}
    acoustid = evidence.get("acoustid") or {}
    itunes = itunes_trusted or {}          # only the *matching* iTunes result
    folder = evidence.get("folder_name")

    candidates = _build_candidates(evidence)
    art = itunes.get("art_url") or shazam.get("art_url")

    if folder_release:
        # Folder artist's own catalog release wins the proposal, and leads the
        # candidate list so the fingerprint's reading is one click away.
        candidates.insert(0, {
            "source": "iTunes (folder artist)",
            "title": folder_release.get("title"), "artist": folder_release.get("artist"),
            "album": folder_release.get("album"), "art_url": folder_release.get("art_url"),
            "preview_url": folder_release.get("preview_url"),
        })
        proposed = {
            "title": folder_release.get("title"),
            "artist": folder_release.get("artist"),
            "album": folder_release.get("album"),
            "art_url": folder_release.get("art_url"),
            "preview_url": folder_release.get("preview_url"),
            "original_artist": primary_artist,      # what the fingerprint claimed
            "source": "iTunes (folder artist)",
        }
        return proposed, candidates

    if verdict in (VERIFIED, LIKELY):
        # Album/art from iTunes only when it matched; else AcoustID's release, never a guess.
        album = itunes.get("album") or shazam.get("album") or acoustid.get("album")
        src = "iTunes" if itunes else ("Shazam" if shazam else "AcoustID")
        proposed = {
            "title": primary_title,
            "artist": primary_artist,
            "album": album,
            "art_url": art,
            "preview_url": itunes.get("preview_url"),
            "source": src,
        }
        return proposed, candidates

    if verdict == COVER:
        proposed = {
            "title": primary_title,
            # Cover artist = the folder/uploader, NOT the original.
            "artist": folder or primary_artist,
            "album": None,                       # never invent an album for a cover
            # §5: never borrow the original recording's artwork onto a cover — blank
            # beats wrong. The file keeps its own embedded art (the uploader's thumb).
            "art_url": None,
            "preview_url": itunes.get("preview_url"),
            "original_artist": primary_artist,   # shown as evidence
            "source": "cover (folder artist)",
        }
        return proposed, candidates

    # UNVERIFIED / NO_MATCH: no pre-filled guess.
    return None, candidates


def _matched_option(shazam, acoustid, itunes_trusted, itunes, primary_title, primary_artist):
    """
    Option 1 ("Matched artist") tags via the trusted-source chain (§3): album +
    art from iTunes only when it matched the identified track, else Shazam, else
    AcoustID's release — never invented. `album_source` names where the album came
    from (for the preview label); `itunes_rejected` is True when iTunes returned a
    *different* song and was dropped (so the UI can say "iTunes had the wrong song").
    """
    it = itunes_trusted or {}
    sh = shazam or {}
    ac = acoustid or {}
    if it.get("album"):
        album, album_source = it["album"], "iTunes"
    elif sh.get("album"):
        album, album_source = sh["album"], "Shazam"
    elif ac.get("album"):
        album, album_source = ac["album"], "AcoustID"
    else:
        album, album_source = None, None
    # Art: iTunes (matched) → Shazam → none. AcoustID carries no artwork; never guess.
    art = it.get("art_url") or sh.get("art_url")
    return {
        "title": primary_title,
        "artist": primary_artist,
        "album": album,
        "album_source": album_source,
        "art_url": art,
        "preview_url": it.get("preview_url"),
        "source": album_source,
        "itunes_rejected": bool(itunes and not itunes_trusted),
    }


def _build_options(evidence, signals, itunes_trusted, primary_title, primary_artist,
                   verdict, filename_original, T):
    """
    Build the four always-present scenario options for the review card (§1). Each
    option's tags are computed up-front so the UI can show exactly what a button
    will write. Cover/original options NEVER borrow the original's art (§5) and use
    a trusted title (§4): the fingerprint title only when it's independently
    corroborated (S1 pass OR S4-title ≥ threshold), otherwise title_from_filename.
    """
    shazam = evidence.get("shazam")
    acoustid = evidence.get("acoustid")
    itunes = evidence.get("itunes")
    folder = evidence.get("folder_name")

    tfn = title_from_filename(filename_original, folder)
    s4_title = (signals["S4"]["value"] or {}).get("title", 0)
    title_trusted = bool(signals["S1"]["pass"] or s4_title >= T)
    trusted_title = primary_title if title_trusted else tfn

    matched = _matched_option(shazam, acoustid, itunes_trusted, itunes,
                              primary_title, primary_artist)
    # Options 2/3: uploader is the artist, no borrowed album/art (§5).
    cover = {"title": trusted_title, "artist": folder or primary_artist,
             "album": None, "art_url": None, "preview_url": None}
    original = {"title": tfn, "artist": folder or primary_artist,
                "album": None, "art_url": None, "preview_url": None}

    # §1 extra option: AcoustID names a DIFFERENT artist for the same recording
    # (the "It's {acoustid_artist}" one-click, e.g. Shazam=Yuzuki Ame / AcoustID=Mochizuki Rei).
    acoustid_alt = None
    if (acoustid and acoustid.get("artist") and primary_artist
            and ratio(acoustid.get("artist"), primary_artist) < T):
        acoustid_alt = {
            "title": acoustid.get("title"), "artist": acoustid.get("artist"),
            "album": acoustid.get("album"), "art_url": None, "preview_url": None,
        }

    # The verdict only chooses which button is pre-highlighted, not what buttons do.
    suggested = {VERIFIED: "matched", LIKELY: "matched", COVER: "cover"}.get(verdict)

    return {
        "matched": matched, "cover": cover, "original": original,
        "title_from_filename": tfn, "title_trusted": title_trusted,
        "acoustid_alt": acoustid_alt, "suggested": suggested,
    }


def _build_candidates(evidence):
    """Assemble selectable options for the UNVERIFIED review card."""
    out = []
    shazam = evidence.get("shazam")
    acoustid = evidence.get("acoustid")
    if shazam:
        out.append({"source": "Shazam", "title": shazam.get("title"),
                    "artist": shazam.get("artist"), "album": shazam.get("album"),
                    "art_url": shazam.get("art_url"), "preview_url": None})
    if acoustid:
        out.append({"source": "AcoustID/MusicBrainz", "title": acoustid.get("title"),
                    "artist": acoustid.get("artist"), "album": acoustid.get("album"),
                    "art_url": None, "preview_url": None})
    for c in (evidence.get("itunes_candidates") or [])[:4]:
        out.append({"source": "iTunes", "title": c.get("title"),
                    "artist": c.get("artist"), "album": c.get("album"),
                    "art_url": c.get("art_url"), "preview_url": c.get("preview_url")})
    return out


def _score(signals, verdict):
    """A rough 0-100 confidence for display/sorting. Not used for tiering."""
    if verdict == NO_MATCH:
        return 0
    pts = 0
    if signals["S1"]["pass"]:
        pts += 35
    elif signals["S1"]["status"] == "red":
        pts -= 20
    if signals["S2"]["pass"]:
        pts += 20
    if signals["S3"]["pass"]:
        pts += 20
    elif signals["S3"]["status"] == "red":
        pts -= 15
    if signals["S4"]["pass"]:
        pts += 15
    if (signals["S6"]["value"] or {}).get("relationship") == "match":
        pts += 10
    if signals["S5"]["pass"]:
        pts -= 10
    return max(0, min(100, pts + 25))

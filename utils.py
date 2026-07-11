#!/usr/bin/env python3
"""
utils.py — text normalization, romaji, filename/folder parsing, duration.

Consolidated + Japanese-aware version of the parsing helpers from Shazamer V2/V3.
Everything here is pure/deterministic so verify.py can rely on it in unit tests.
"""

import logging
import os
import re
import unicodedata
from urllib.parse import unquote

logger = logging.getLogger("earditor.utils")

# --- Romaji (lazy pykakasi) ----------------------------------------------------
_kks = None


def _kakasi():
    global _kks
    if _kks is None:
        try:
            import pykakasi
            _kks = pykakasi.kakasi()
        except Exception as e:  # pragma: no cover
            logger.warning("pykakasi unavailable (%s); romaji disabled", e)
            _kks = False
    return _kks


def to_romaji(text):
    """
    Convert Japanese text to Hepburn romaji. Non-Japanese passes through unchanged.
    '夜に駆ける' -> 'yoru ni kakeru'. Returns lowercase, space-joined.
    """
    if not text:
        return ""
    kks = _kakasi()
    if not kks:
        return text.lower()
    try:
        parts = kks.convert(text)
        out = " ".join(p["hepburn"] for p in parts if p.get("hepburn"))
        out = re.sub(r"\s+", " ", out).strip().lower()
        return out or text.lower()
    except Exception:
        return text.lower()


def has_japanese(text):
    if not text:
        return False
    return any(
        "぀" <= c <= "ゟ"     # hiragana
        or "゠" <= c <= "ヿ"  # katakana
        or "一" <= c <= "鿿"  # kanji
        for c in text
    )


# --- Noise stripping / normalization -------------------------------------------
_NOISE_PATTERNS = [
    r"【[^】]*】", r"〖[^〗]*〗", r"「[^」]*」", r"『[^』]*』",  # JP brackets
    r"\[[^\]]*\]",                                            # [ ... ]
    r"\((?:official|lyric|audio|mv|m/v|visualizer|hd|4k|uhd)[^)]*\)",
    r"\b(?:official\s+)?music\s+video\b",
    r"\bofficial\s+(?:audio|mv|video|lyric video)\b",
    r"\blyric\s+video\b", r"\bvisualizer\b", r"\bcreditless\b",
    r"\b(?:UHD|4K|60FPS|1080p|720p|HD)\b",
    r"\b(?:OP|ED|OST|AMV|MAD)\d*\b",           # anime opening/ending/OST tags
    r"@\w+",                                     # handles
    r"[\U0001F000-\U0010FFFF]",                # emoji / astral
    r"\s*[\(\[][A-Za-z0-9_\-]{8,12}[\)\]]",    # yt-dlp video ids
]
_NOISE_RE = [re.compile(p, re.IGNORECASE) for p in _NOISE_PATTERNS]


def normalize(text):
    """
    NFKC-normalize, lowercase, strip diacritics-safe punctuation to spaces.
    Used for fuzzy comparison so 'Yoru ni Kakeru' ~ 'yoru ni kakeru'.
    Keeps CJK intact.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    # separators -> space
    text = re.sub(r"[—–\-_/|•·,:;~=+]", " ", text)
    # drop remaining punctuation but keep word chars + CJK + spaces
    text = re.sub(r"[^\w\s぀-ヿ一-鿿]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_title(text):
    """Strip bracket/format noise from a raw title/filename (display + search)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", unquote(text))
    for rx in _NOISE_RE:
        text = rx.sub(" ", text)
    text = re.sub(r"[—–|_]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def filename_tokens(filepath):
    """
    Clean filename into a normalized token string (for S4 corroboration).
    Returns dict: {'raw': cleaned display, 'norm': normalized, 'romaji': romaji}.
    """
    base = os.path.splitext(os.path.basename(filepath))[0]
    cleaned = clean_title(base)
    norm = normalize(cleaned)
    romaji = to_romaji(cleaned) if has_japanese(cleaned) else norm
    # 'original' keeps bracket content (URL-decoded, NFKC) so cover-keyword
    # detection can see 【歌ってみた】 / 【ENGLISH COVER】 that clean_title strips.
    original = unicodedata.normalize("NFKC", unquote(base))
    return {"raw": cleaned, "norm": norm, "romaji": romaji, "original": original}


def parse_filename_for_display(filepath):
    """Human-readable cleaned filename for the review card."""
    return clean_title(os.path.splitext(os.path.basename(filepath))[0])


# --- Title recovered from the filename (§4 trusted-title fallback) --------------
# The keyword data lives in covers.py so adding a word there also improves this.
import covers as _covers  # noqa: E402  (local import avoids any import-order surprise)

# ASCII cover/version phrases to peel out of a filename, longest-first so
# "english ver" is removed before the bare "ver". CJK keywords are handled by
# clean_title (它 strips 【…】) and the substring pass below.
_COVER_PHRASES = sorted(
    (k for k in _covers.COVER_KEYWORDS if k.isascii()),
    key=len, reverse=True,
)
_CJK_COVER_PHRASES = [k for k in _covers.COVER_KEYWORDS if not k.isascii()]
# Free-standing version markers that are never part of a song title.
_VER_MARKER_RE = re.compile(r"\b(?:ver(?:sion)?|cv)\.?\b", re.IGNORECASE)
_ENG_VER_RE = re.compile(r"\benglish\b", re.IGNORECASE)
_SEP_SPLIT_RE = re.compile(r"[\-–—/|｜／・·•]")
_EDGE_JUNK = " -–—/|｜／・·•~\t"
_BRACKET_CHARS_RE = re.compile(r"[()（）\[\]「」『』【】〖〗｟｠]")
# A separator-segment that is really a SOURCE/context tag (anime OP/ED/OST, a
# "theme song" credit) rather than the song title — dropped before choosing.
_SOURCE_SEG_RE = re.compile(
    r"\b(?:OP|ED|OST|AMV|MAD|PV|MV|BGM)\d*\b"
    r"|\b(?:opening|ending|theme\s*song|insert\s*song|character\s*song)\b",
    re.IGNORECASE,
)


def _strip_phrase(text, phrase):
    """Remove a phrase from text on word boundaries (ASCII) or as a substring (CJK)."""
    if not phrase:
        return text
    if phrase.isascii():
        return re.sub(rf"\b{re.escape(phrase)}\b", " ", text, flags=re.IGNORECASE)
    return text.replace(phrase, " ")


def _folder_variants(folder):
    """Uploader-name forms to strip: the whole thing plus any parenthetical alias.
    'HoshiCovers (nekomelody)' → ['HoshiCovers (nekomelody)', 'HoshiCovers', 'nekomelody']."""
    if not folder:
        return []
    out = [folder]
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", folder)
    if m:
        out += [m.group(1).strip(), m.group(2).strip()]
    return [v for v in out if v]


def _strip_markers(text, folder):
    """Peel uploader name + cover/version keywords + bracket noise out of a segment."""
    text = clean_title(text)                        # brackets/format/OP-ED-OST noise
    for name in _folder_variants(folder):           # uploader / channel identity
        text = _strip_phrase(text, name)
    for kw in _CJK_COVER_PHRASES:                    # 歌ってみた / カバー etc. (if any survived)
        text = text.replace(kw, " ")
    for kw in _COVER_PHRASES:                         # cover / nightcore / english ver …
        text = _strip_phrase(text, kw)
    text = _ENG_VER_RE.sub(" ", text)                # stray "English" left by "English Cover"
    text = _VER_MARKER_RE.sub(" ", text)             # ver. / version / cv.
    # Removing keywords from inside brackets leaves empty/hanging wrappers
    # ("Idol ( )", "Kawaki wo Ameku ("). Brackets in filenames are noise here, so drop them.
    text = _BRACKET_CHARS_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(_EDGE_JUNK).strip()


def title_from_filename(filename, folder=None):
    """
    Best-effort SONG TITLE recovered from a raw filename, for use when the
    fingerprint's own title can't be trusted (§4). Splits on separators FIRST, then
    drops segments that are the uploader/folder name or a source tag (anime OP/ED/
    OST, "theme song"), cleans the rest of cover/version keywords + bracket noise,
    and returns the longest remaining segment. Never returns "" — falls back to the
    cleaned whole filename. Deterministic.
    """
    if not filename:
        return ""
    base = os.path.splitext(os.path.basename(filename))[0]
    base = unicodedata.normalize("NFKC", unquote(base))

    # Drop source-tag segments (OP/ED/OST) outright, then let _strip_markers peel
    # the uploader name out of the remaining segments; a segment that is *only* the
    # uploader/markers cleans to empty and is skipped by the letter check below.
    kept = []
    for raw in _SEP_SPLIT_RE.split(base):
        seg = raw.strip(_EDGE_JUNK).strip()
        if not seg or _SOURCE_SEG_RE.search(seg):
            continue
        cleaned = _strip_markers(seg, folder)
        if cleaned and re.search(r"[^\W\d_]", cleaned, re.UNICODE):
            kept.append(cleaned)
    if kept:
        return max(kept, key=len)
    # Nothing survived segmentation — clean the whole filename as a fallback.
    return _strip_markers(base, folder) or clean_title(base)


def has_mojibake(text):
    """Detect encoding corruption (garbled non-CJK high-bytes)."""
    if not text:
        return False
    if "�" in text:
        return True
    non_ascii = sum(1 for c in text if ord(c) > 127)
    if len(text) > 3 and non_ascii / len(text) > 0.4:
        cjk = sum(1 for c in text if has_japanese(c) or "가" <= c <= "힯")
        if cjk == 0 and non_ascii > 2:
            return True
    return False


def raw_folder_name(filepath):
    """
    The iTunes artist folder from a path, UNSTRIPPED — i.e. a trailing '- Topic'
    suffix is kept so callers can detect YouTube Music auto channels.
    Structure: .../Music/<ArtistFolder>/<Album>/track.mp3
    Returns None for Unknown folders. (No mojibake filtering here — see below.)
    """
    parts = filepath.replace("\\", "/").split("/")
    # Prefer the folder above an album dir; else the grandparent of the file.
    candidate = None
    for i, part in enumerate(parts):
        if part.lower() in ("unknown album", "unknown", "album") and i > 0:
            candidate = parts[i - 1]
            break
    if candidate is None and len(parts) >= 3:
        candidate = parts[-3]  # grandparent

    if not candidate:
        return None
    if candidate.lower() in ("unknown artist", "unknown", "music", "media.localized"):
        return None
    return candidate


def extract_folder_name(filepath):
    """
    Get the iTunes artist folder from a path, with a trailing '- Topic' suffix
    stripped (the channel's artist identity). Returns None for Unknown/mojibake
    folders. For the raw, suffix-preserving form use raw_folder_name().
    """
    candidate = raw_folder_name(filepath)
    if not candidate:
        return None
    candidate = re.sub(r"\s*-\s*topic\s*$", "", candidate, flags=re.IGNORECASE).strip()
    if not candidate or has_mojibake(candidate):
        return None
    return candidate


# --- Duration ------------------------------------------------------------------
# Placeholder tag values that mean "not really tagged" (Music.app defaults).
_PLACEHOLDER_TAGS = {"", "unknown", "unknown artist", "unknown album", "untitled"}


def read_existing_tags(filepath):
    """Return {'title','artist','album'} from the file's existing tags (or Nones)."""
    try:
        import mutagen
        mf = mutagen.File(filepath, easy=True)
        if mf is None:
            return {"title": None, "artist": None, "album": None}
        def first(key):
            v = mf.get(key)
            return v[0] if v else None
        return {"title": first("title"), "artist": first("artist"), "album": first("album")}
    except Exception as e:
        logger.debug("tag read failed for %s: %s", filepath, e)
        return {"title": None, "artist": None, "album": None}


def is_well_tagged(filepath, tags=None):
    """
    True if the file already has real title + artist + album tags (not placeholders).
    These are files that don't need re-identification — official/already-processed.
    """
    t = tags or read_existing_tags(filepath)
    for key in ("title", "artist", "album"):
        val = (t.get(key) or "").strip().lower()
        if not val or val in _PLACEHOLDER_TAGS:
            return False
    return True


# High-precision "this title was never cleaned" markers — raw YouTube upload cruft
# only. Deliberately NOT including "feat."/length: those appear in legit titles.
_TITLE_JUNK = ["【", "】", "official audio", "official mv", "official video",
               "lyric video", "m/v", "[mv]", "[audio]", "visualizer",
               "(official", "color coded"]


def tags_look_messy(filepath, tags=None):
    """
    True if a file's EXISTING tags look bad even though all three fields are filled —
    e.g. mojibake, a title that's really the raw filename, or leftover video junk.
    These *should* be re-identified (unlike clean tags, which we trust and skip).
    Conservative on purpose: better to skip a clean file than re-review it.
    """
    t = tags or read_existing_tags(filepath)
    title = (t.get("title") or "").strip()
    artist = (t.get("artist") or "").strip()
    album = (t.get("album") or "").strip()

    if any(has_mojibake(x) for x in (title, artist, album)):
        return True
    low = title.lower()
    if any(tok in low for tok in _TITLE_JUNK):
        return True
    # Title is basically the raw filename (never cleaned) AND the filename itself is
    # noisy (has a source separator) — a plain "Song Name" that happens to match its
    # filename is fine.
    fn = os.path.splitext(os.path.basename(filepath))[0]
    if normalize(title) and normalize(title) == normalize(fn) and (" - " in fn or "_" in fn):
        return True
    return False


def get_duration(filepath):
    """File duration in seconds via mutagen, or None."""
    try:
        import mutagen
        mf = mutagen.File(filepath)
        if mf is not None and mf.info is not None and getattr(mf.info, "length", None):
            return float(mf.info.length)
    except Exception as e:
        logger.debug("duration read failed for %s: %s", filepath, e)
    return None

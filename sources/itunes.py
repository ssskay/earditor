#!/usr/bin/env python3
"""
sources/itunes.py — iTunes Search API (free, no key).

Provides:
  - catalog confirmation: does (artist, title) exist as a real release? (S2)
  - canonical album name + 1200x1200 artwork (fixes bad-album/art failure mode)
  - a 30-second previewUrl for the listen-and-compare review UI
  - track duration for the duration-sanity signal (S3)
  - a candidate list (search by title alone) for UNVERIFIED review
"""

import logging
import re
import time

import requests

logger = logging.getLogger("earditor.itunes")

_ART_RE = re.compile(r"\d+x\d+bb")


def _bump_art(url, size=1200):
    if not url:
        return None
    return _ART_RE.sub(f"{size}x{size}bb", url)


def _normalize(r):
    """Map a raw iTunes result to our shape."""
    millis = r.get("trackTimeMillis")
    return {
        "title": r.get("trackName"),
        "artist": r.get("artistName"),
        "album": r.get("collectionName"),
        "art_url": _bump_art(r.get("artworkUrl100")),
        "preview_url": r.get("previewUrl"),
        "duration": (millis / 1000.0) if millis else None,
        "track_view_url": r.get("trackViewUrl"),
    }


class iTunesSource:
    BASE = "https://itunes.apple.com/search"

    def __init__(self, delay=0.3):
        self.delay = delay

    def _get(self, term, limit):
        try:
            resp = requests.get(
                self.BASE,
                params={"term": term, "media": "music", "entity": "song", "limit": limit},
                timeout=10,
            )
            resp.raise_for_status()
            if self.delay:
                time.sleep(self.delay)
            return resp.json().get("results", [])
        except Exception as e:
            logger.debug("iTunes search error for '%s': %s", term, e)
            return []

    def search_track(self, artist, title):
        """
        Confirm a specific (artist, title). Query 'artist title', return the best
        result as our normalized dict, or None. Used for S2 catalog confirmation.
        """
        term = " ".join(x for x in (artist, title) if x).strip()
        if not term:
            return None
        results = self._get(term, limit=5)
        if not results:
            return None
        return _normalize(results[0])

    def search_candidates(self, term, limit=5):
        """
        Query by title (or free text) alone, return a list of normalized candidates
        for the UNVERIFIED review card. De-duplicated by (artist, title, album).
        """
        results = self._get(term, limit=limit)
        out, seen = [], set()
        for r in results:
            n = _normalize(r)
            key = (n["artist"], n["title"], n["album"])
            if key in seen:
                continue
            seen.add(key)
            out.append(n)
        return out

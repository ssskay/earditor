#!/usr/bin/env python3
"""
sources/shazam.py — shazamio wrapper (async, maintained).

Primary audio fingerprint. Returns title, artist, album, and high-res art URL.
Uses the modern `shazamio` package (NOT the dead `ShazamAPI`). Each call runs
the async recognizer via asyncio.run so scan.py can stay synchronous.
"""

import asyncio
import logging

logger = logging.getLogger("earditor.shazam")


class ShazamSource:
    def __init__(self):
        try:
            from shazamio import Shazam
            self._shazam = Shazam()
            self.enabled = True
        except Exception as e:  # pragma: no cover
            logger.warning("shazamio unavailable: %s", e)
            self._shazam = None
            self.enabled = False

    def recognize(self, filepath):
        """
        Fingerprint a file. Returns a normalized dict or None (no match).

        {
          'title': str, 'artist': str, 'album': str|None,
          'art_url': str|None, 'genre': str|None,
          'apple_music_url': str|None, 'shazam_key': str|None,
        }
        """
        if not self.enabled:
            return None
        try:
            data = asyncio.run(self._shazam.recognize(str(filepath)))
        except Exception as e:
            logger.debug("Shazam recognize error for %s: %s", filepath, e)
            return None

        return self._parse(data)

    @staticmethod
    def _parse(data):
        if not data:
            return None
        # No fingerprint match → shazamio returns {'matches': [], ...}
        if not data.get("matches"):
            return None
        track = data.get("track") or {}
        if not track:
            return None

        title = (track.get("title") or "").strip()
        artist = (track.get("subtitle") or "").strip()  # Shazam stores artist in 'subtitle'
        if not title:
            return None

        # Album lives in a SONG section's metadata list.
        album = None
        genre = None
        for section in track.get("sections", []) or []:
            if section.get("type") == "SONG":
                for item in section.get("metadata", []) or []:
                    if item.get("title") == "Album" and not album:
                        album = (item.get("text") or "").strip() or None
            meta = section.get("metadata") or []
            for item in meta:
                if item.get("title") in ("Genre",) and not genre:
                    genre = item.get("text")
        # Genre also commonly at track.genres.primary
        if not genre:
            genre = (track.get("genres") or {}).get("primary")

        images = track.get("images") or {}
        art_url = images.get("coverarthq") or images.get("coverart") or None

        apple_url = None
        for opt in ((track.get("hub") or {}).get("options") or []):
            for action in opt.get("actions", []) or []:
                if action.get("type") == "applemusicplay" and action.get("uri"):
                    apple_url = action["uri"]
                    break

        return {
            "title": title,
            "artist": artist or None,
            "album": album,
            "art_url": art_url,
            "genre": genre,
            "apple_music_url": apple_url,
            "shazam_key": track.get("key"),
        }

#!/usr/bin/env python3
"""
sources/acoustid_mb.py — independent fingerprint via AcoustID + MusicBrainz.

This is the *independent* fingerprint that lets us catch "wrong song entirely":
AcoustID computes a Chromaprint fingerprint (via fpcalc) and looks it up against
MusicBrainz, returning recording title/artist/duration and release (album) info.

Degrades gracefully:
  - no API key       -> disabled, returns None
  - fpcalc missing   -> logs once, returns None
  - lookup failure   -> returns None
"""

import logging

logger = logging.getLogger("earditor.acoustid")


class AcoustIDSource:
    def __init__(self, api_key, min_score=0.5):
        self.api_key = api_key
        self.min_score = min_score
        try:
            import acoustid
            self._acoustid = acoustid
            self.enabled = bool(api_key)
        except Exception as e:  # pragma: no cover
            logger.warning("pyacoustid unavailable: %s", e)
            self._acoustid = None
            self.enabled = False
        if api_key is None:
            logger.info("AcoustID disabled (no API key)")

    def identify(self, filepath):
        """
        Fingerprint + lookup. Returns best recording match dict or None.

        {
          'score': float, 'recording_id': str,
          'title': str, 'artist': str,
          'duration': float|None,
          'album': str|None,               # best release title
          'releases': [{'title','date'}],  # up to a few candidates
        }
        """
        if not self.enabled:
            return None
        ac = self._acoustid
        try:
            duration, fingerprint = ac.fingerprint_file(str(filepath))
        except ac.FingerprintGenerationError as e:
            logger.debug("fpcalc fingerprint failed for %s: %s", filepath, e)
            return None
        except Exception as e:
            logger.debug("fingerprint error for %s: %s", filepath, e)
            return None

        try:
            data = ac.lookup(self.api_key, fingerprint, duration,
                             meta="recordings releases")
        except ac.WebServiceError as e:
            logger.debug("AcoustID web error: %s", e)
            return None
        except Exception as e:
            logger.debug("AcoustID lookup error: %s", e)
            return None

        # Surface API errors instead of silently treating them as "no match".
        if isinstance(data, dict) and data.get("status") == "error":
            err = data.get("error", {}) or {}
            code, msg = err.get("code"), err.get("message")
            if code == 4 or (msg and "api key" in str(msg).lower()):
                # Invalid/expired key: disable for the rest of the run so we don't
                # hammer the API and don't mislabel every S1 as "no fingerprint".
                logger.error(
                    "AcoustID API key rejected (%s). Disabling AcoustID for this run — "
                    "S1 (fingerprint agreement) will be unavailable. "
                    "Fix: store a valid AcoustID *application* API key in Keychain "
                    "(security add-generic-password -s acoustid -a \"$USER\" -w <KEY> -U).",
                    msg,
                )
                self.enabled = False
            else:
                logger.warning("AcoustID error %s: %s", code, msg)
            return None

        return self._parse(data)

    def _parse(self, data):
        if not data or data.get("status") != "ok":
            return None
        results = data.get("results") or []
        if not results:
            return None

        best = None
        for res in results:
            score = res.get("score", 0.0)
            if score < self.min_score:
                continue
            for rec in res.get("recordings", []) or []:
                title = (rec.get("title") or "").strip()
                if not title:
                    continue
                artists = rec.get("artists") or []
                artist = " & ".join(a.get("name", "") for a in artists).strip(" &") or None
                releases = []
                for rel in (rec.get("releases") or [])[:5]:
                    releases.append({
                        "title": rel.get("title"),
                        "date": (rel.get("date") or {}).get("year")
                                if isinstance(rel.get("date"), dict) else rel.get("date"),
                    })
                candidate = {
                    "score": score,
                    "recording_id": rec.get("id"),
                    "title": title,
                    "artist": artist,
                    "duration": rec.get("duration"),
                    "album": releases[0]["title"] if releases else None,
                    "releases": releases,
                }
                if best is None or candidate["score"] > best["score"]:
                    best = candidate
        return best

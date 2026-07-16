"""Tests for the /api/align endpoint.

Uses Flask's test client. The preview download and the (heavy) align_audio call are
monkeypatched, and track validation is stubbed, so the route's control flow and JSON
shape are tested without network, ffmpeg, or a real DB.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review  # noqa: E402
import align   # noqa: E402


class AlignEndpointTest(unittest.TestCase):
    def setUp(self):
        review.DEMO = False
        self.client = review.app.test_client()

    def _get(self, path="/x.mp3", preview="http://itunes/p.m4a"):
        from urllib.parse import urlencode
        return self.client.get("/api/align?" + urlencode({"path": path, "preview": preview}))

    def test_happy_path_returns_offset_and_label(self):
        with mock.patch.object(review.db, "get_track", return_value={"filepath": "/x.mp3"}), \
             mock.patch.object(review.os.path, "isfile", return_value=True), \
             mock.patch.object(review, "_fetch_preview", return_value="/tmp/p.m4a"), \
             mock.patch.object(align, "align_audio",
                               return_value={"offset_sec": 12.3, "confidence": 0.82}):
            r = self._get()
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertAlmostEqual(data["offset"], 12.3)
        self.assertEqual(data["label"], "strong")

    def test_missing_preview_param(self):
        with mock.patch.object(review.db, "get_track", return_value={"filepath": "/x.mp3"}), \
             mock.patch.object(review.os.path, "isfile", return_value=True):
            r = self._get(preview="")
        self.assertEqual(r.get_json(), {"ok": False, "reason": "no_preview"})

    def test_unknown_track(self):
        with mock.patch.object(review.db, "get_track", return_value=None):
            r = self._get()
        self.assertEqual(r.get_json()["reason"], "unknown_track")

    def test_download_failure(self):
        with mock.patch.object(review.db, "get_track", return_value={"filepath": "/x.mp3"}), \
             mock.patch.object(review.os.path, "isfile", return_value=True), \
             mock.patch.object(review, "_fetch_preview", return_value=None):
            r = self._get()
        self.assertEqual(r.get_json()["reason"], "download_failed")

    def test_decode_failure(self):
        with mock.patch.object(review.db, "get_track", return_value={"filepath": "/x.mp3"}), \
             mock.patch.object(review.os.path, "isfile", return_value=True), \
             mock.patch.object(review, "_fetch_preview", return_value="/tmp/p.m4a"), \
             mock.patch.object(align, "align_audio", side_effect=RuntimeError("boom")):
            r = self._get()
        self.assertEqual(r.get_json()["reason"], "decode_failed")

    def test_librosa_missing_is_unavailable(self):
        with mock.patch.object(review.db, "get_track", return_value={"filepath": "/x.mp3"}), \
             mock.patch.object(review.os.path, "isfile", return_value=True), \
             mock.patch.object(review, "_fetch_preview", return_value="/tmp/p.m4a"), \
             mock.patch.object(align, "align_audio", side_effect=ImportError("no librosa")):
            r = self._get()
        self.assertEqual(r.get_json()["reason"], "unavailable")

    def test_demo_mode_is_unavailable(self):
        review.DEMO = True
        try:
            r = self._get()
        finally:
            review.DEMO = False
        self.assertEqual(r.get_json(), {"ok": False, "reason": "unavailable"})

    def test_align_import_failure_is_unavailable(self):
        # Simulates a demo install where numpy (and therefore align.py, which
        # imports it at module scope) isn't installed: `import align` itself must
        # raise ImportError, not just align_audio(). sys.modules["align"] = None
        # makes `import align` raise ImportError without touching real numpy state.
        sys.modules["align"] = None
        self.addCleanup(lambda: sys.modules.__setitem__("align", align))
        with mock.patch.object(review.db, "get_track", return_value={"filepath": "/x.mp3"}), \
             mock.patch.object(review.os.path, "isfile", return_value=True):
            r = self._get()
        self.assertEqual(r.get_json(), {"ok": False, "reason": "unavailable"})


if __name__ == "__main__":
    unittest.main()

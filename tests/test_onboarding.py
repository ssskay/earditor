"""Onboarding: the first-run state machine's data, /api/config, and /api/stats.

The three empty-queue states (welcome / report card / celebration) are selected
client-side, but every discriminator comes from /api/stats — so these tests pin
the numbers the UI keys off, per state:

    total == 0                     -> welcome card
    pending > filters.total        -> report card with "Start identifying"
    pending > 0, all filtered out  -> report card with the skip message
    pending == 0                   -> the 🎉 celebration (now earned)
"""

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import db  # noqa: E402
import review  # noqa: E402


class ApiTestCase(unittest.TestCase):
    """A Flask test client wired to a throwaway DB and config.json."""

    def setUp(self):
        self._td = TemporaryDirectory()
        self.root = Path(self._td.name)
        self.db_path = self.root / "test.db"
        self.config_path = self.root / "config.json"
        self.music = self.root / "Music"
        self.music.mkdir()

        db.init_db(str(self.db_path)).close()
        self._patches = [
            mock.patch.object(review, "DB_PATH", self.db_path),
            mock.patch.object(config, "CONFIG_PATH", self.config_path),
            mock.patch.object(review, "cfg", config.Config(
                {**config.DEFAULTS, "music_path": str(self.music)})),
            mock.patch.object(review, "DEMO", False),
            mock.patch.object(review, "FOLDER_PICKER", None),
        ]
        for p in self._patches:
            p.start()
        review.app.config["TESTING"] = True
        self.client = review.app.test_client()
        self.addCleanup(self._td.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)

    def conn(self):
        return db.connect(str(self.db_path))

    def track(self, *parts):
        return str(self.music.joinpath(*parts))

    def add(self, path, status="pending"):
        c = self.conn()
        db.add_pending_bulk(c, [path])   # _bulk commits; add_pending alone does not
        if status == "tagged":
            db.mark_tagged(c, path, {"title": "T", "artist": "A", "album": "B"})
        elif status == "accepted":
            db.mark_accepted(c, path, {"title": "T", "artist": "A"})
        c.close()

    def stats(self):
        return self.client.get("/api/stats").get_json()


class StatsShapeTest(ApiTestCase):
    def test_fresh_database_reports_an_empty_library(self):
        # No rows at all: the welcome card's condition.
        s = self.stats()
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["pending"], 0)
        self.assertEqual(s["clean"], 0)

    def test_payload_carries_every_field_the_home_screen_reads(self):
        self.add(self.track("a.mp3"))
        s = self.stats()
        for key in ("total", "clean", "pending", "status_counts", "filters", "music_path"):
            self.assertIn(key, s)
        for key in ("excluded", "outside", "total"):
            self.assertIn(key, s["filters"])

    def test_clean_counts_triage_retirements_and_accepts_but_not_pending(self):
        self.add(self.track("tagged.mp3"), "tagged")
        self.add(self.track("accepted.mp3"), "accepted")
        self.add(self.track("todo.mp3"))
        s = self.stats()
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["clean"], 2)      # green half of the split bar
        self.assertEqual(s["pending"], 1)

    def test_work_remaining_selects_the_report_card_not_a_celebration(self):
        self.add(self.track("tagged.mp3"), "tagged")
        self.add(self.track("todo.mp3"))
        s = self.stats()
        self.assertGreater(s["pending"], s["filters"]["total"])   # → "Start identifying"

    def test_all_done_selects_the_celebration(self):
        self.add(self.track("tagged.mp3"), "tagged")
        s = self.stats()
        self.assertEqual(s["pending"], 0)
        self.assertEqual(s["total"], 1)


class FilterSkipTest(ApiTestCase):
    """An empty queue caused by path filters must say so, not celebrate."""

    def _exclude(self, *dirs):
        review.cfg = config.Config({
            **config.DEFAULTS,
            "music_path": str(self.music),
            "exclude_paths": [str(self.music / d) for d in dirs],
        })

    def test_excluded_pending_files_are_reported_as_excluded(self):
        self.add(self.track("Podcasts", "ep1.mp3"))
        self.add(self.track("Podcasts", "ep2.mp3"))
        self._exclude("Podcasts")
        s = self.stats()
        self.assertEqual(s["pending"], 2)
        self.assertEqual(s["filters"]["excluded"], 2)
        # pending - filters.total == 0 → the UI shows the skip message, no button.
        self.assertEqual(s["pending"] - s["filters"]["total"], 0)

    def test_pending_outside_music_path_is_counted_separately(self):
        # A stale registration from a previous music_path.
        self.add(str(self.root / "Elsewhere" / "old.mp3"))
        s = self.stats()
        self.assertEqual(s["filters"]["outside"], 1)
        self.assertEqual(s["filters"]["excluded"], 0)

    def test_partly_filtered_library_still_offers_identification(self):
        self.add(self.track("Podcasts", "ep.mp3"))
        self.add(self.track("Music", "song.mp3"))
        self._exclude("Podcasts")
        s = self.stats()
        self.assertEqual(s["filters"]["total"], 1)
        self.assertEqual(s["pending"] - s["filters"]["total"], 1)   # → still a button


class ConfigApiTest(ApiTestCase):
    def post_config(self, **body):
        return self.client.post("/api/config", json=body)

    def test_get_reports_the_current_folder_and_picker_availability(self):
        r = self.client.get("/api/config").get_json()
        self.assertEqual(r["music_path_resolved"], str(self.music))
        self.assertTrue(r["music_path_exists"])
        self.assertFalse(r["can_pick_folder"])      # no native window in a browser

    def test_get_flags_a_folder_that_no_longer_exists(self):
        review.cfg = config.Config({**config.DEFAULTS,
                                    "music_path": str(self.root / "gone")})
        self.assertFalse(self.client.get("/api/config").get_json()["music_path_exists"])

    def test_a_path_that_is_not_a_directory_is_rejected(self):
        for bad in (str(self.root / "nope"), ""):
            with self.subTest(path=bad):
                r = self.post_config(music_path=bad)
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.get_json()["error"], "not_a_directory")

    def test_a_file_is_not_a_music_folder(self):
        f = self.root / "song.mp3"
        f.write_bytes(b"")
        self.assertEqual(self.post_config(music_path=str(f)).status_code, 400)

    def test_a_rejected_path_leaves_the_live_config_untouched(self):
        self.post_config(music_path=str(self.root / "nope"))
        self.assertEqual(review.cfg.music_path, str(self.music))
        self.assertFalse(self.config_path.exists())

    def test_tilde_is_expanded_for_validation_but_stored_portably(self):
        new = self.music / "Sub"
        new.mkdir()
        # expanduser reads HOME on POSIX and USERPROFILE on Windows — set both so
        # this runs the same on the macOS and Windows CI legs.
        home = {"HOME": str(self.music), "USERPROFILE": str(self.music)}
        with mock.patch.dict(os.environ, home):
            r = self.post_config(music_path="~/Sub")
        self.assertEqual(r.status_code, 200)
        # The raw "~/Sub" persists (portable across machines)...
        self.assertEqual(json.loads(self.config_path.read_text())["music_path"], "~/Sub")
        # ...and resolves to the real directory when read.
        with mock.patch.dict(os.environ, home):
            self.assertEqual(config.load_config(self.config_path).music_path, str(new))

    def test_the_new_folder_is_persisted_and_picked_up_without_a_restart(self):
        new = self.root / "Other"
        new.mkdir()
        r = self.post_config(music_path=str(new))
        self.assertTrue(r.get_json()["ok"])
        # on disk...
        self.assertEqual(json.loads(self.config_path.read_text())["music_path"], str(new))
        # ...and in the live process, which the next scan reads music_path from.
        self.assertEqual(review.cfg.music_path, str(new))
        self.assertEqual(self.stats()["music_path"], str(new))

    def test_config_json_is_seeded_from_the_example_keeping_its_comments(self):
        new = self.root / "Other"
        new.mkdir()
        self.post_config(music_path=str(new))
        written = json.loads(self.config_path.read_text())
        self.assertIn("_comment", written)                   # the documented example
        self.assertIn("thresholds", written)

    def test_an_existing_config_keeps_its_other_keys(self):
        self.config_path.write_text(json.dumps(
            {"music_path": "/old", "playlist_name": "Mine", "scan_max_files": 7}))
        new = self.root / "Other"
        new.mkdir()
        self.post_config(music_path=str(new))
        written = json.loads(self.config_path.read_text())
        self.assertEqual(written["playlist_name"], "Mine")
        self.assertEqual(written["scan_max_files"], 7)

    def test_only_whitelisted_keys_can_be_written(self):
        new = self.root / "Other"
        new.mkdir()
        self.post_config(music_path=str(new), playlist_name="hijacked",
                         use_llm_tiebreaker=True)
        written = json.loads(self.config_path.read_text())
        self.assertNotEqual(written.get("playlist_name"), "hijacked")
        self.assertFalse(written.get("use_llm_tiebreaker"))

    def test_a_body_with_no_editable_key_is_a_bad_request(self):
        self.assertEqual(self.post_config(playlist_name="x").status_code, 400)

    def test_demo_mode_can_never_repoint_a_real_library(self):
        new = self.root / "Other"
        new.mkdir()
        with mock.patch.object(review, "DEMO", True):
            self.assertEqual(self.post_config(music_path=str(new)).status_code, 400)
        self.assertFalse(self.config_path.exists())


class FolderPickerTest(ApiTestCase):
    def test_without_a_native_window_the_picker_is_unavailable(self):
        r = self.client.post("/api/config/pick_folder")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "no_window")

    def test_the_injected_picker_returns_the_chosen_folder(self):
        with mock.patch.object(review, "FOLDER_PICKER", lambda: "/picked/folder"):
            r = self.client.post("/api/config/pick_folder").get_json()
        self.assertEqual(r, {"ok": True, "path": "/picked/folder"})

    def test_cancelling_the_dialog_is_not_an_error(self):
        with mock.patch.object(review, "FOLDER_PICKER", lambda: None):
            r = self.client.post("/api/config/pick_folder").get_json()
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "cancelled")


class JobStatusTest(ApiTestCase):
    def test_status_reports_files_not_cards(self):
        s = self.client.get("/api/scan/status").get_json()
        for key in ("job", "running", "files_done", "files_total",
                    "cards_ready", "secs_per_file"):
            self.assertIn(key, s)

    def test_rate_is_the_rolling_average_since_the_run_started(self):
        with mock.patch.object(review, "JOB", {
            **review.JOB, "job": "scan", "running": True,
            "started_at": time.time() - 60, "files_done": 10, "files_total": 100,
            "cards_ready": 3,
        }):
            s = self.client.get("/api/scan/status").get_json()
        self.assertAlmostEqual(s["secs_per_file"], 6.0, places=1)   # "~6s per file"

    def test_rate_is_absent_before_the_first_file_lands(self):
        with mock.patch.object(review, "JOB", {
            **review.JOB, "running": True, "started_at": time.time(), "files_done": 0,
        }):
            self.assertIsNone(self.client.get("/api/scan/status").get_json()["secs_per_file"])

    def test_an_unknown_job_type_is_rejected(self):
        r = self.client.post("/api/scan/start", json={"job": "rm -rf"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "unknown_job")

    def test_only_one_job_runs_at_a_time(self):
        with mock.patch.object(review, "JOB", {**review.JOB, "running": True}):
            r = self.client.post("/api/scan/start", json={"job": "triage"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "already_running")


class AcceptDuringScanTest(ApiTestCase):
    """Reviewing while a scan runs is the whole point of "Review N now" — SQLite
    WAL plus the 30s busy timeout has to make the concurrent write land."""

    def test_a_track_can_be_accepted_while_a_scan_writes_to_the_database(self):
        fp = self.track("song.mp3")
        self.add(fp)
        c = self.conn()
        db.save_scan_result(c, fp, {"verdict": "LIKELY",
                                    "proposed": {"title": "T", "artist": "A"}})
        c.close()

        stop = threading.Event()
        errors = []

        def scan_like_writer():
            """Stand in for scan.py: a steady stream of commits from another thread."""
            w = self.conn()
            try:
                i = 0
                while not stop.is_set():
                    other = self.track(f"scanning_{i}.mp3")
                    db.add_pending_bulk(w, [other])
                    db.save_scan_result(w, other, {"verdict": "VERIFIED",
                                                   "proposed": {"title": "x"}})
                    i += 1
            except Exception as e:      # a lock error here is the regression
                errors.append(e)
            finally:
                w.close()

        writer = threading.Thread(target=scan_like_writer, daemon=True)
        writer.start()
        try:
            time.sleep(0.05)            # let the writer get going
            with mock.patch.object(review, "DEMO", True):   # no real tag writes
                with mock.patch.object(review, "JOB", {**review.JOB,
                                                       "running": True, "job": "scan"}):
                    r = self.client.post("/api/accept", json={
                        "filepath": fp, "tags": {"title": "T", "artist": "A"}})
        finally:
            stop.set()
            writer.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertTrue(r.get_json()["ok"])
        c = self.conn()
        self.assertEqual(db.get_track(c, fp)["status"], "accepted")
        c.close()


if __name__ == "__main__":
    unittest.main()

"""Configuration and state-location regression tests."""

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import db


class DataLocationTest(TestCase):
    def test_data_dir_environment_override(self):
        with TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"EARDITOR_DATA_DIR": td}
        ):
            self.assertEqual(config._data_dir(), Path(td).resolve())

    def test_frozen_state_dirs_are_per_platform(self):
        env = {k: v for k, v in os.environ.items() if k != "EARDITOR_DATA_DIR"}
        cases = [
            ("darwin", {}, Path.home() / "Library" / "Application Support" / "Earditor"),
            ("win32", {"APPDATA": os.path.join("C:", "Roaming")},
             Path(os.path.join("C:", "Roaming")) / "Earditor"),
            ("win32", {}, Path.home() / "AppData" / "Roaming" / "Earditor"),
            ("linux", {}, Path.home() / ".local" / "share" / "Earditor"),
        ]
        for platform, extra, expected in cases:
            scrubbed = {k: v for k, v in env.items() if k not in ("APPDATA", "XDG_DATA_HOME")}
            with self.subTest(platform=platform, extra=bool(extra)):
                with (
                    mock.patch.dict(os.environ, {**scrubbed, **extra}, clear=True),
                    mock.patch.object(config.sys, "platform", platform),
                    mock.patch.object(config.sys, "frozen", True, create=True),
                ):
                    self.assertEqual(config._data_dir(), expected)

    def test_source_checkout_keeps_repo_local_state(self):
        env = {k: v for k, v in os.environ.items() if k != "EARDITOR_DATA_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config._data_dir(), config.SOURCE_DIR)

    def test_shazamer_database_migrates_without_losing_sidecars(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            old = root / "shazamer.db"
            new = root / "earditor.db"
            old.write_bytes(b"sqlite-state")
            Path(f"{old}-wal").write_bytes(b"wal-state")
            with (
                mock.patch.object(config, "DATA_DIR", root),
                mock.patch.object(config, "DB_PATH", new),
                mock.patch.object(config, "_LEGACY_DB_PATHS", (old,)),
            ):
                config.migrate_db_filename()

            self.assertEqual(new.read_bytes(), b"sqlite-state")
            self.assertEqual(Path(f"{new}-wal").read_bytes(), b"wal-state")
            self.assertFalse(old.exists())

    def test_existing_earditor_database_is_never_overwritten(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            old = root / "shazamer.db"
            new = root / "earditor.db"
            old.write_bytes(b"old")
            new.write_bytes(b"current")
            with (
                mock.patch.object(config, "DATA_DIR", root),
                mock.patch.object(config, "DB_PATH", new),
                mock.patch.object(config, "_LEGACY_DB_PATHS", (old,)),
            ):
                config.migrate_db_filename()

            self.assertEqual(new.read_bytes(), b"current")
            self.assertTrue(old.exists())

    def test_non_application_database_does_not_trigger_name_migration(self):
        with TemporaryDirectory() as td, mock.patch.object(
            config, "migrate_db_filename"
        ) as migrate:
            conn = db.connect(str(Path(td) / "demo.db"))
            conn.close()
        migrate.assert_not_called()

    def test_legacy_default_playlist_name_upgrades_in_memory(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text('{"playlist_name": "Shazamer — Tagged"}', encoding="utf-8")
            loaded = config.load_config(path)
        self.assertEqual(loaded.playlist_name, "Earditor — Tagged")


class ResourceRootTest(TestCase):
    def test_resourcepath_env_wins_for_py2app(self):
        with mock.patch.dict(os.environ, {"RESOURCEPATH": "/bundle/Resources"}):
            self.assertEqual(config.resource_root(), Path("/bundle/Resources"))

    def test_meipass_is_used_for_pyinstaller(self):
        env = {k: v for k, v in os.environ.items() if k != "RESOURCEPATH"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(config.sys, "_MEIPASS", "/tmp/_MEI123", create=True),
        ):
            self.assertEqual(config.resource_root(), Path("/tmp/_MEI123"))

    def test_source_checkout_uses_the_source_dir(self):
        env = {k: v for k, v in os.environ.items() if k != "RESOURCEPATH"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.resource_root(), config.SOURCE_DIR)


class EnsureFpcalcTest(TestCase):
    def test_explicit_fpcalc_env_is_never_overridden(self):
        with mock.patch.dict(os.environ, {"FPCALC": "/usr/local/bin/fpcalc"}):
            self.assertEqual(config.ensure_fpcalc(), "/usr/local/bin/fpcalc")

    def test_vendored_binary_is_found_and_exported(self):
        env = {k: v for k, v in os.environ.items() if k != "FPCALC"}
        with TemporaryDirectory() as td:
            vendored = Path(td) / "fpcalc"
            vendored.write_bytes(b"#!/bin/sh\n")
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(config.sys, "platform", "darwin"),
                mock.patch.object(config, "resource_root", lambda: Path(td)),
            ):
                self.assertEqual(config.ensure_fpcalc(), str(vendored))
                self.assertEqual(os.environ["FPCALC"], str(vendored))

    def test_windows_looks_for_the_exe(self):
        env = {k: v for k, v in os.environ.items() if k != "FPCALC"}
        with TemporaryDirectory() as td:
            (Path(td) / "fpcalc.exe").write_bytes(b"MZ")
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(config.sys, "platform", "win32"),
                mock.patch.object(config, "resource_root", lambda: Path(td)),
            ):
                self.assertEqual(config.ensure_fpcalc(), str(Path(td) / "fpcalc.exe"))

    def test_missing_binary_degrades_to_none_rather_than_raising(self):
        env = {k: v for k, v in os.environ.items() if k != "FPCALC"}
        with TemporaryDirectory() as td, mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(config, "resource_root", lambda: Path(td)):
            self.assertIsNone(config.ensure_fpcalc())
            self.assertNotIn("FPCALC", os.environ)


class MusicAppIntegrationTest(TestCase):
    """The platform check must win over config.json, in both directions."""

    def _cfg(self, **overrides):
        return config.Config({**config.DEFAULTS, **overrides})

    def test_enabled_by_default_on_macos(self):
        with mock.patch.object(config.sys, "platform", "darwin"):
            self.assertTrue(self._cfg().music_app_integration)

    def test_config_can_turn_it_off_on_macos(self):
        with mock.patch.object(config.sys, "platform", "darwin"):
            self.assertFalse(self._cfg(music_app_integration=False).music_app_integration)

    def test_forced_off_on_windows_even_when_config_asks_for_it(self):
        with mock.patch.object(config.sys, "platform", "win32"):
            self.assertFalse(self._cfg(music_app_integration=True).music_app_integration)

    def test_forced_off_on_linux(self):
        with mock.patch.object(config.sys, "platform", "linux"):
            self.assertFalse(self._cfg().music_app_integration)


class AcoustIDKeyTest(TestCase):
    def test_environment_key_wins_and_never_shells_out(self):
        with (
            mock.patch.dict(os.environ, {"ACOUSTID_API_KEY": "  envkey  "}),
            mock.patch.object(config.subprocess, "run") as run,
        ):
            self.assertEqual(config.get_acoustid_key(), "envkey")
        run.assert_not_called()

    def test_keychain_is_not_consulted_off_macos(self):
        env = {k: v for k, v in os.environ.items() if k != "ACOUSTID_API_KEY"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(config.sys, "platform", "win32"),
            mock.patch.object(config.subprocess, "run") as run,
        ):
            self.assertIsNone(config.get_acoustid_key())
        run.assert_not_called()

    def test_no_key_warning_off_macos_does_not_mention_keychain(self):
        env = {k: v for k, v in os.environ.items() if k != "ACOUSTID_API_KEY"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(config.sys, "platform", "win32"),
            self.assertLogs("earditor.config", level="WARNING") as logs,
        ):
            config.get_acoustid_key()
        self.assertNotIn("keychain", "\n".join(logs.output).lower())


ROOT = os.path.abspath("/lib")


def track(*parts):
    """An absolute track path under the fake library root."""
    return os.path.join(ROOT, *parts)


def allowed(path, include=(), exclude=()):
    return config.path_allowed(path, ROOT, include, exclude)


class PathFilterTest(TestCase):
    """`path_allowed` is pure: no disk access, no cwd dependence."""

    def test_empty_include_admits_everything_under_music_path(self):
        self.assertTrue(allowed(track("Artist", "Album", "a.mp3")))

    def test_path_outside_music_path_is_never_allowed(self):
        outside = os.path.abspath("/elsewhere/Artist/a.mp3")
        self.assertFalse(allowed(outside))
        # ...not even when an include names it explicitly.
        self.assertFalse(allowed(outside, include=[os.path.abspath("/elsewhere")]))

    def test_include_admits_only_its_own_subtree(self):
        inc = [track("Keep")]
        self.assertTrue(allowed(track("Keep", "Album", "a.mp3"), include=inc))
        self.assertFalse(allowed(track("Other", "Album", "a.mp3"), include=inc))

    def test_include_matches_a_parent_directory_not_just_exact_path(self):
        # The pattern names a directory; files nested any depth below it match.
        self.assertTrue(
            allowed(track("Keep", "Album", "Disc 1", "a.mp3"), include=[track("Keep")])
        )

    def test_sibling_with_a_shared_name_prefix_is_not_a_child(self):
        # "/lib/Keep" must not swallow "/lib/Keep Extra".
        self.assertFalse(
            allowed(track("Keep Extra", "a.mp3"), include=[track("Keep")])
        )

    def test_glob_suffixes_are_stripped_from_patterns(self):
        for pattern in (track("Keep") + "/**", track("Keep") + "/*", track("Keep")):
            with self.subTest(pattern=pattern):
                self.assertTrue(
                    allowed(track("Keep", "Album", "a.mp3"), include=[pattern])
                )

    def test_exclude_wins_over_include(self):
        self.assertFalse(
            allowed(
                track("Keep", "Live", "a.mp3"),
                include=[track("Keep")],
                exclude=[track("Keep", "Live")],
            )
        )

    def test_exclude_applies_with_no_include_configured(self):
        self.assertFalse(allowed(track("Podcasts", "ep.mp3"), exclude=[track("Podcasts")]))
        self.assertTrue(allowed(track("Music", "a.mp3"), exclude=[track("Podcasts")]))

    def test_matching_is_case_insensitive(self):
        self.assertTrue(allowed(track("KEEP", "a.mp3"), include=[track("keep")]))
        self.assertFalse(allowed(track("keep", "a.mp3"), exclude=[track("KEEP")]))

    def test_relative_patterns_resolve_against_music_path(self):
        self.assertTrue(allowed(track("Keep", "a.mp3"), include=["Keep"]))
        self.assertFalse(allowed(track("Podcasts", "a.mp3"), exclude=["Podcasts"]) )

    def test_music_path_itself_as_include_admits_the_whole_library(self):
        self.assertTrue(allowed(track("Any", "a.mp3"), include=[ROOT]))

    def test_filter_defaults_are_empty_lists(self):
        cfg = config.load_config(Path("/nonexistent/config.json"))
        self.assertEqual(cfg.include_paths, [])
        self.assertEqual(cfg.exclude_paths, [])


class PendingFilterTest(TestCase):
    """The predicate must run BEFORE the limit, or --limit under-delivers."""

    def _conn(self, td, paths):
        conn = db.init_db(str(Path(td) / "demo.db"))
        db.add_pending_bulk(conn, paths)
        return conn

    def test_predicate_filters_before_limit(self):
        # 3 excluded rows sort first; a naive "LIMIT 2 then filter" returns 0.
        paths = [
            track("Excluded", "1.mp3"),
            track("Excluded", "2.mp3"),
            track("Excluded", "3.mp3"),
            track("Keep", "4.mp3"),
            track("Keep", "5.mp3"),
        ]
        with TemporaryDirectory() as td:
            conn = self._conn(td, paths)
            got = db.get_pending(
                conn,
                limit=2,
                predicate=lambda p: allowed(p, exclude=[track("Excluded")]),
            )
            conn.close()
        self.assertEqual(got, [track("Keep", "4.mp3"), track("Keep", "5.mp3")])

    def test_get_pending_without_predicate_is_unchanged(self):
        paths = [track("a.mp3"), track("b.mp3")]
        with TemporaryDirectory() as td:
            conn = self._conn(td, paths)
            self.assertEqual(db.get_pending(conn), sorted(paths))
            self.assertEqual(db.get_pending(conn, limit=1), [sorted(paths)[0]])
            conn.close()

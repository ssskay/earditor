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

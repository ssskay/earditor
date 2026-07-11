#!/usr/bin/env python3
"""
migrate_v3.py — import a previous version's exclude.csv into earditor.db.

Each line in exclude.csv is a file that was already processed in V1/V2/V3. We
record those as status='accepted' so Earditor never rescans them.

Handles both formats seen across versions:
  - absolute paths:  /Users/.../Music/Artist/Unknown Album/track.mp3   (V3)
  - relative paths:  Artist/Unknown Album/track.mp3                    (V2)  → resolved
    against music_path.

The merged legacy list ships in-house as `legacy_accepted.csv` (deduped, absolute
paths, built from V1/V2/V3 exclude.csv), so this repo never needs to read the old
project folders again.

Usage:
    python3 migrate_v3.py                      # default: ./legacy_accepted.csv
    python3 migrate_v3.py path/to/exclude.csv  # any other exclude file
    python3 migrate_v3.py --no-verify-exists   # import even if the file is gone
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from config import load_config, DB_PATH
import db

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("earditor.migrate")

DEFAULT_CSV = Path(__file__).resolve().parent / "legacy_accepted.csv"


def resolve_path(line, music_path):
    line = line.strip().strip('"')
    if not line:
        return None
    if os.path.isabs(line):
        return os.path.normpath(line)
    # Relative form: join under music_path.
    return os.path.normpath(os.path.join(music_path, line))


def main():
    ap = argparse.ArgumentParser(description="Import exclude.csv as accepted tracks")
    ap.add_argument("csv", nargs="?", default=str(DEFAULT_CSV))
    ap.add_argument("--no-verify-exists", action="store_true",
                    help="import lines even if the file no longer exists on disk")
    args = ap.parse_args()

    cfg = load_config()
    csv_path = Path(args.csv)
    if not csv_path.exists():
        log.error("exclude.csv not found: %s", csv_path)
        sys.exit(1)

    conn = db.init_db(str(DB_PATH))
    imported = missing = blank = 0

    with open(csv_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            fp = resolve_path(raw, cfg.music_path)
            if not fp:
                blank += 1
                continue
            if not args.no_verify_exists and not os.path.exists(fp):
                missing += 1
                log.debug("missing on disk: %s", fp)
                continue
            db.mark_accepted_from_csv(conn, fp)
            imported += 1

    conn.commit()
    conn.close()
    log.info("Imported %d accepted track(s) from %s", imported, csv_path.name)
    if missing:
        log.info("Skipped %d line(s) whose files no longer exist "
                 "(use --no-verify-exists to import anyway)", missing)
    if blank:
        log.info("Skipped %d blank line(s)", blank)


if __name__ == "__main__":
    main()

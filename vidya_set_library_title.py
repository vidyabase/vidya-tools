#!/usr/bin/env python3
"""
Show or change the library title of a .vidya library.

Sets `db_meta.library_title` (and bumps `db_meta.modified_at`). Run without a
title to print the current one.

Creates a timestamped backup before modifying the database.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from vidya_lib.item_ids import connect_vidya, set_db_meta, table_exists, utc_now_rfc3339


def _backup_path(vidya_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return vidya_path.with_name(f"{vidya_path.name}.bak-{stamp}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Show or change the library title of a .vidya library.")
    parser.add_argument("vidya", type=Path, help="path to the .vidya file")
    parser.add_argument("title", nargs="?", help="new library title (omit to print the current title)")
    parser.add_argument("--dry-run", action="store_true", help="show the change only; no backup, no writes")
    args = parser.parse_args()

    path: Path = args.vidya
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if not table_exists(conn, "db_meta"):
            print("error: not a vidya library (missing db_meta)", file=sys.stderr)
            return 1
        row = conn.execute("SELECT value FROM db_meta WHERE key = 'library_title'").fetchone()
    finally:
        conn.close()
    current = row[0] if row else None

    if args.title is None:
        print(current if current is not None else "(no library_title set)")
        return 0

    title = args.title.strip()
    if not title:
        print("error: title must not be empty", file=sys.stderr)
        return 1

    print(f"current: {current!r}" if current is not None else "current: (not set)")
    print(f"new:     {title!r}")
    if title == current:
        print("title unchanged: no changes made")
        return 0
    if args.dry_run:
        print("dry run: no changes made")
        return 0

    backup = _backup_path(path)
    shutil.copy2(path, backup)
    print(f"backup: {backup}")

    conn = connect_vidya(path)
    try:
        with conn:
            set_db_meta(conn, "library_title", title)
            set_db_meta(conn, "modified_at", utc_now_rfc3339())
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    print(f"library title updated: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

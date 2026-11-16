#!/usr/bin/env python3
"""
Shift global item ids in a .vidya library (advanced / merge maintenance).

Requires a timestamped backup before modifying the database.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from vidya_lib.item_ids import (
    ENTITY_TABLES,
    FTS_TABLES,
    REFERENCE_COLUMNS,
    WIKILINK_ID_TARGET_ALT_RE,
    WIKILINK_ID_TARGET_RE,
    bump_column_values,
    collect_all_item_ids,
    compute_shift_set,
    connect_vidya,
    count_rows_for_shift,
    find_collisions,
    item_id_in_shift_set,
    max_entity_id,
    table_exists,
    utc_now_rfc3339,
)


def _backup_path(vidya_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return vidya_path.with_name(f"{vidya_path.name}.bak-{stamp}")


def _patch_wikilinks(
    conn,
    id_offset: int,
    *,
    after_id: int | None,
    start_id: int | None,
) -> int:
    updated = 0
    rows = conn.execute(
        "SELECT id, content FROM records WHERE content LIKE '%data-target-type=\"id\"%'"
    )
    for row_id, content in rows:
        new_content = _patch_wikilink_content(
            content,
            id_offset,
            after_id=after_id,
            start_id=start_id,
        )
        if new_content != content:
            conn.execute(
                "UPDATE records SET content = ? WHERE id = ?",
                (new_content, row_id),
            )
            updated += 1
    return updated


def _patch_wikilink_content(
    content: str,
    id_offset: int,
    *,
    after_id: int | None,
    start_id: int | None,
) -> str:
    def shift_target(old: int) -> int | None:
        if not item_id_in_shift_set(old, after_id=after_id, start_id=start_id):
            return None
        return old + id_offset

    def repl_primary(match: re.Match[str]) -> str:
        old = int(match.group(2))
        new = shift_target(old)
        if new is None:
            return match.group(0)
        return f"{match.group(1)}{new}{match.group(3)}"

    def repl_alt(match: re.Match[str]) -> str:
        old = int(match.group(2))
        new = shift_target(old)
        if new is None:
            return match.group(0)
        return f"{match.group(1)}{new}{match.group(3)}"

    content = WIKILINK_ID_TARGET_RE.sub(repl_primary, content)
    content = WIKILINK_ID_TARGET_ALT_RE.sub(repl_alt, content)
    return content


def _update_db_meta(conn) -> None:
    next_id = str(max_entity_id(conn) + 1)
    now = utc_now_rfc3339()
    for key, value in (("editor_next_item_id", next_id), ("modified_at", now)):
        row = conn.execute("SELECT 1 FROM db_meta WHERE key = ?", (key,)).fetchone()
        if row:
            conn.execute("UPDATE db_meta SET value = ? WHERE key = ?", (value, key))
        else:
            conn.execute("INSERT INTO db_meta (key, value) VALUES (?, ?)", (key, value))


def run(
    vidya_path: Path,
    id_offset: int,
    *,
    after_id: int | None,
    start_id: int | None,
    dry_run: bool,
) -> int:
    if not vidya_path.is_file():
        print(f"Error: not a file: {vidya_path}", file=sys.stderr)
        return 2

    if id_offset == 0:
        print("Error: id_offset must not be 0 (no-op).", file=sys.stderr)
        return 2

    conn = connect_vidya(vidya_path)
    try:
        all_ids = collect_all_item_ids(conn)
        shift_set = compute_shift_set(all_ids, after_id=after_id, start_id=start_id)

        if not shift_set:
            print("Nothing to shift: no item ids match the given cutoff.")
            return 0

        if any(i + id_offset <= 0 for i in shift_set):
            print(
                "Error: id_offset would produce a non-positive id for at least one item.",
                file=sys.stderr,
            )
            return 1

        collisions = find_collisions(all_ids, shift_set, id_offset)
        if collisions:
            print("Error: shift would collide with ids that are not being moved:", file=sys.stderr)
            for c in collisions[:20]:
                print(
                    f"  id {c.old_id} would become {c.target_id}, "
                    f"but {c.target_id} exists and is outside the shift set",
                    file=sys.stderr,
                )
            if len(collisions) > 20:
                print(f"  ... and {len(collisions) - 20} more", file=sys.stderr)
            return 1

        counts = count_rows_for_shift(conn, after_id=after_id, start_id=start_id)
        print(f"Item ids to shift: {len(shift_set)} (id_offset={id_offset:+d})")
        for label, n in sorted(counts.items()):
            print(f"  {label}: {n}")

        if dry_run:
            print("Dry run: no backup and no changes made.")
            return 0

        backup = _backup_path(vidya_path)
        shutil.copy2(vidya_path, backup)
        print(f"Backup: {backup}")

        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in ENTITY_TABLES:
                if not table_exists(conn, table):
                    continue
                bump_column_values(
                    conn,
                    table,
                    "id",
                    id_offset,
                    after_id=after_id,
                    start_id=start_id,
                )

            for table, column in REFERENCE_COLUMNS:
                if not table_exists(conn, table):
                    continue
                bump_column_values(
                    conn,
                    table,
                    column,
                    id_offset,
                    after_id=after_id,
                    start_id=start_id,
                )

            for fts in FTS_TABLES:
                if not table_exists(conn, fts):
                    continue
                bump_column_values(
                    conn,
                    fts,
                    "item_id",
                    id_offset,
                    after_id=after_id,
                    start_id=start_id,
                )

            wikilink_rows = _patch_wikilinks(
                conn,
                id_offset,
                after_id=after_id,
                start_id=start_id,
            )
            if wikilink_rows:
                print(f"Updated wikilink targets in {wikilink_rows} record(s).")

            _update_db_meta(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        print("Done.")
        return 0
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Shift global item ids in a .vidya file (advanced maintenance).",
    )
    parser.add_argument("vidya_file", type=Path, help="Path to the .vidya library")
    parser.add_argument(
        "--id-offset",
        type=int,
        required=True,
        help="Value added to each shifted id (e.g. 200)",
    )
    cutoff = parser.add_mutually_exclusive_group(required=True)
    cutoff.add_argument(
        "--after-id",
        type=int,
        metavar="N",
        help="Shift ids with id > N (N=0 means all ids with id > 0)",
    )
    cutoff.add_argument(
        "--start-id",
        type=int,
        metavar="N",
        help="Shift ids with id >= N (N=0 means all ids with id > 0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check collisions and print row counts only",
    )
    args = parser.parse_args()

    after_id = args.after_id
    start_id = args.start_id
    if after_id is not None and after_id < 0:
        parser.error("--after-id must be >= 0")
    if start_id is not None and start_id < 0:
        parser.error("--start-id must be >= 0")

    sys.exit(
        run(
            args.vidya_file,
            args.id_offset,
            after_id=after_id,
            start_id=start_id,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()

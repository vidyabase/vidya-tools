#!/usr/bin/env python3
"""
Enable quick navigation in a .vidya library.

Builds the `quick_nav` table from `content_tree` (books, chapters, verses by
default) and sets `db_meta.quick_nav = 'true'`. Re-running rebuilds the table.

Each quick-nav row mirrors a tree node:
  id         = item_id of the node
  parent_id  = item_id of the nearest ancestor that is also in quick nav (NULL at root)
  sort_order = position among its quick-nav siblings (tree order)
  code       = the node's slug, or last url_paths segment (what the user types / taps)
  url_path   = reader route, e.g. /chapter/20437

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

from vidya_lib.item_ids import connect_vidya, table_exists, utc_now_rfc3339

DEFAULT_TYPES = ("book", "chapter", "verse")

QUICK_NAV_SCHEMA = """
CREATE TABLE quick_nav (
    id         INTEGER PRIMARY KEY,
    parent_id  INTEGER,  -- NULL for root
    sort_order INTEGER DEFAULT 0,
    code       TEXT NOT NULL,
    url_path   TEXT NOT NULL
)
"""


def _backup_path(vidya_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return vidya_path.with_name(f"{vidya_path.name}.bak-{stamp}")


def build_rows(conn, types: set[str]) -> tuple[list[tuple], int]:
    """Walk content_tree in order; return quick_nav rows and count of skipped nodes."""
    children: dict[int | None, list[tuple]] = {}
    # content_tree.parent_id references the parent's item_id (not content_tree.id).
    for parent_id, type_, item_id, slug in conn.execute(
        "SELECT parent_id, type, item_id, slug FROM content_tree "
        "ORDER BY parent_id, sort_order, id"
    ):
        children.setdefault(parent_id, []).append((type_, item_id, slug))

    # Legacy files have no content_tree.slug; fall back to the last url_paths segment.
    path_slugs: dict[int, str] = {}
    if table_exists(conn, "url_paths"):
        for url_path, item_id in conn.execute("SELECT url_path, item_id FROM url_paths"):
            path_slugs.setdefault(item_id, url_path.rstrip("/").rsplit("/", 1)[-1])

    rows: list[tuple] = []
    skipped = 0
    sibling_counter: dict[int | None, int] = {}
    # Iterative DFS: (tree parent item_id, nearest quick-nav ancestor item_id)
    stack: list[tuple[int | None, int | None]] = [(None, None)]
    while stack:
        tree_parent, qn_parent = stack.pop()
        pending = []
        for type_, item_id, slug in children.get(tree_parent, []):
            next_qn_parent = qn_parent
            if type_ in types:
                code = (slug or path_slugs.get(item_id) or "").strip()
                if code:
                    order = sibling_counter.get(qn_parent, 0)
                    sibling_counter[qn_parent] = order + 1
                    rows.append((item_id, qn_parent, order, code, f"/{type_}/{item_id}"))
                    next_qn_parent = item_id
                else:
                    skipped += 1
            pending.append((item_id, next_qn_parent))
        # Push in reverse so siblings are visited in sort order (keeps
        # sibling_counter consistent with tree order).
        stack.extend(reversed(pending))
    return rows, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description="Enable quick navigation in a .vidya library.")
    parser.add_argument("vidya", type=Path, help="path to the .vidya file")
    parser.add_argument(
        "--types",
        default=",".join(DEFAULT_TYPES),
        help=f"comma-separated content_tree types to include (default: {','.join(DEFAULT_TYPES)})",
    )
    parser.add_argument("--dry-run", action="store_true", help="show counts only; no backup, no writes")
    args = parser.parse_args()

    path: Path = args.vidya
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1
    types = {t.strip() for t in args.types.split(",") if t.strip()}

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table in ("content_tree", "db_meta"):
            if not table_exists(conn, table):
                print(f"error: not a vidya library (missing {table})", file=sys.stderr)
                return 1
        rows, skipped = build_rows(conn, types)
    finally:
        conn.close()

    roots = sum(1 for r in rows if r[1] is None)
    print(f"quick_nav rows: {len(rows)} ({roots} at root), types: {', '.join(sorted(types))}")
    if skipped:
        print(f"skipped {skipped} node(s) without a slug")
    if not rows:
        print("error: nothing to add to quick nav", file=sys.stderr)
        return 1
    if args.dry_run:
        for r in rows[:10]:
            print(f"  {r}")
        print("dry run: no changes made")
        return 0

    backup = _backup_path(path)
    shutil.copy2(path, backup)
    print(f"backup: {backup}")

    conn = connect_vidya(path)
    try:
        with conn:
            conn.execute("DROP TABLE IF EXISTS quick_nav")
            conn.execute(QUICK_NAV_SCHEMA)
            conn.execute("CREATE INDEX idx_quick_nav_parent ON quick_nav(parent_id, sort_order)")
            conn.executemany(
                "INSERT INTO quick_nav (id, parent_id, sort_order, code, url_path) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute(
                "INSERT INTO db_meta (key, value) VALUES ('quick_nav', 'true') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            conn.execute(
                "INSERT INTO db_meta (key, value) VALUES ('modified_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (utc_now_rfc3339(),),
            )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    print(f"quick nav enabled: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

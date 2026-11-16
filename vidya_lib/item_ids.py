"""Global item_id helpers for .vidya (SQLite) maintenance tools."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# §8 global id pool — primary keys in these tables share one sequence.
ENTITY_TABLES: tuple[str, ...] = (
    "books",
    "chapters",
    "verses",
    "sections",
    "footnote_containers",
    "transcripts",
    "letters",
    "records",
    "image_assets",
    "images",
)

# (table, column) references to item_ids (not entity PK updates).
REFERENCE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("content_tree", "item_id"),
    ("content_tree", "parent_id"),
    ("url_paths", "item_id"),
    ("books", "parent_id"),
    ("chapters", "parent_id"),
    ("verses", "parent_id"),
    ("sections", "parent_id"),
    ("footnote_containers", "parent_id"),
    ("transcripts", "parent_id"),
    ("letters", "parent_id"),
    ("images", "asset_id"),
    ("author_links", "item_id"),
)

FTS_TABLES: tuple[str, ...] = ("search_index", "content_fts")

WIKILINK_ID_TARGET_RE = re.compile(
    r'(<a\b[^>]*\bclass="wikilink"[^>]*\bdata-target-type="id"[^>]*\bdata-target=")'
    r"(\d+)"
    r'(")',
    re.IGNORECASE,
)
WIKILINK_ID_TARGET_ALT_RE = re.compile(
    r'(<a\b[^>]*\bclass="wikilink"[^>]*\bdata-target=")'
    r"(\d+)"
    r'("[^>]*\bdata-target-type="id")',
    re.IGNORECASE,
)


def connect_vidya(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def collect_all_item_ids(conn: sqlite3.Connection) -> set[int]:
    ids: set[int] = set()
    for table in ENTITY_TABLES:
        if not table_exists(conn, table):
            continue
        for (value,) in conn.execute(f"SELECT id FROM {table} WHERE id IS NOT NULL"):
            ids.add(int(value))
    return ids


def item_id_in_shift_set(
    item_id: int,
    *,
    after_id: int | None,
    start_id: int | None,
) -> bool:
    if after_id is not None:
        if after_id == 0:
            return item_id > 0
        return item_id > after_id
    if start_id is not None:
        if start_id == 0:
            return item_id > 0
        return item_id >= start_id
    raise ValueError("either after_id or start_id must be set")


def compute_shift_set(
    all_ids: Iterable[int],
    *,
    after_id: int | None,
    start_id: int | None,
) -> set[int]:
    return {
        i
        for i in all_ids
        if item_id_in_shift_set(i, after_id=after_id, start_id=start_id)
    }


@dataclass(frozen=True)
class Collision:
    old_id: int
    target_id: int


def find_collisions(
    all_ids: set[int],
    shift_set: set[int],
    id_offset: int,
) -> list[Collision]:
    collisions: list[Collision] = []
    for old_id in shift_set:
        target = old_id + id_offset
        if target in all_ids and target not in shift_set:
            collisions.append(Collision(old_id=old_id, target_id=target))
    collisions.sort(key=lambda c: c.old_id)
    return collisions


def max_entity_id(conn: sqlite3.Connection) -> int:
    max_id = 0
    for table in ENTITY_TABLES:
        if not table_exists(conn, table):
            continue
        row = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()
        if row and row[0] is not None:
            max_id = max(max_id, int(row[0]))
    return max_id


def utc_now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shift_predicate_sql(
    column: str,
    *,
    after_id: int | None,
    start_id: int | None,
) -> tuple[str, tuple[int, ...]]:
    """SQL fragment and params for rows whose value is in the shift set."""
    if after_id is not None:
        if after_id == 0:
            return f"{column} > ?", (0,)
        return f"{column} > ?", (after_id,)
    if start_id is not None:
        if start_id == 0:
            return f"{column} > ?", (0,)
        return f"{column} >= ?", (start_id,)
    raise ValueError("either after_id or start_id must be set")


def bump_column_values(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    id_offset: int,
    *,
    after_id: int | None,
    start_id: int | None,
) -> int:
    """Add id_offset to column values in the shift set (two-pass, collision-safe)."""
    where_sql, params = shift_predicate_sql(column, after_id=after_id, start_id=start_id)
    cur = conn.execute(
        f"UPDATE {table} SET {column} = -({column} + ?) WHERE {where_sql}",
        (id_offset, *params),
    )
    changed = cur.rowcount
    if changed:
        conn.execute(f"UPDATE {table} SET {column} = -{column} WHERE {column} < 0")
    return changed


def count_rows_for_shift(
    conn: sqlite3.Connection,
    *,
    after_id: int | None,
    start_id: int | None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ENTITY_TABLES:
        if not table_exists(conn, table):
            continue
        where_sql, params = shift_predicate_sql("id", after_id=after_id, start_id=start_id)
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where_sql}",
            params,
        ).fetchone()[0]
        if n:
            counts[f"{table}.id"] = n
    for table, column in REFERENCE_COLUMNS:
        if not table_exists(conn, table):
            continue
        where_sql, params = shift_predicate_sql(column, after_id=after_id, start_id=start_id)
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where_sql}",
            params,
        ).fetchone()[0]
        if n:
            counts[f"{table}.{column}"] = n
    for fts in FTS_TABLES:
        if not table_exists(conn, fts):
            continue
        where_sql, params = shift_predicate_sql("item_id", after_id=after_id, start_id=start_id)
        n = conn.execute(
            f"SELECT COUNT(*) FROM {fts} WHERE {where_sql}",
            params,
        ).fetchone()[0]
        if n:
            counts[f"{fts}.item_id"] = n
    if table_exists(conn, "records"):
        counts["records.content (wikilink scan)"] = conn.execute(
            "SELECT COUNT(*) FROM records WHERE content LIKE '%data-target-type=\"id\"%'"
        ).fetchone()[0]
    return counts

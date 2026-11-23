#!/usr/bin/env python3
"""
Make an SQL patch that turns one .vidya (SQLite) file into another.

    python3 vidya_sql_diff.py original.vidya new.vidya -o patch.sql
    sqlite3 original.vidya < patch.sql      # original now matches new

The patch covers:
  * schema: tables, virtual tables (FTS), indexes, views, triggers
    - a table whose CREATE statement changed is dropped, recreated and refilled
  * data: per-row DELETE / UPDATE / INSERT, matched by primary key
    (rowid for tables without a primary key and for virtual tables)
  * sqlite_sequence (AUTOINCREMENT counters) and PRAGMA user_version

FTS shadow tables are not diffed directly: the FTS table itself is, so the
index stays consistent. Values are compared exactly (type and value), so an
integer 1 and a text '1' count as different.

The patch runs in one transaction with foreign keys off. Use --verify to apply
it to a temporary copy of ORIGINAL and check the result matches NEW.

Neither input file is modified.
"""

from __future__ import annotations

import argparse
import io
import math
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, TextIO

ORIG = "main"
NEW = "newdb"

# Shadow-table suffixes per virtual-table module (fallback for SQLite < 3.37).
SHADOW_SUFFIXES = (
    "_data", "_idx", "_content", "_docsize", "_config",  # fts5
    "_segments", "_segdir", "_stat",  # fts3/4
    "_node", "_rowid", "_parent",  # rtree
)


# --------------------------------------------------------------------------- SQL helpers


def qi(name: str) -> str:
    """Quote an identifier."""
    return '"' + name.replace('"', '""') + '"'


def lit(value) -> str:
    """Render a Python value as an SQLite literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NULL"
        if math.isinf(value):
            return "9e999" if value > 0 else "-9e999"
        return repr(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "X'" + bytes(value).hex().upper() + "'"
    s = str(value)
    if "\x00" in s:
        return "(" + " || char(0) || ".join(lit(p) for p in s.split("\x00")) + ")"
    return "'" + s.replace("'", "''") + "'"


def key_match(cols: list[str], values: Iterable) -> str:
    parts = []
    for c, v in zip(cols, values):
        parts.append(f"{qi(c)} IS NULL" if v is None else f"{qi(c)} = {lit(v)}")
    return " AND ".join(parts)


# --------------------------------------------------------------------------- schema


@dataclass
class SchemaObj:
    type: str  # table | index | view | trigger
    name: str
    tbl_name: str
    sql: str
    virtual: bool = False


@dataclass
class Schema:
    objects: dict[str, SchemaObj] = field(default_factory=dict)  # by name, in creation order

    def of_type(self, type_: str) -> dict[str, SchemaObj]:
        return {n: o for n, o in self.objects.items() if o.type == type_}


def _shadow_tables(conn: sqlite3.Connection, schema: str, virtual_names: set[str]) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA {schema}.table_list").fetchall()
        return {r[1] for r in rows if r[2] == "shadow"}
    except sqlite3.DatabaseError:
        shadows = set()
        for (name,) in conn.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'"):
            for v in virtual_names:
                if name.startswith(v + "_") and name[len(v):] in SHADOW_SUFFIXES:
                    shadows.add(name)
        return shadows


def read_schema(conn: sqlite3.Connection, schema: str, exclude: set[str]) -> Schema:
    rows = conn.execute(
        f"SELECT type, name, tbl_name, sql FROM {schema}.sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY rowid"
    ).fetchall()
    virtual = {
        name for type_, name, _, sql in rows
        if type_ == "table" and sql.lstrip().upper().startswith("CREATE VIRTUAL")
    }
    shadows = _shadow_tables(conn, schema, virtual)
    out = Schema()
    for type_, name, tbl_name, sql in rows:
        if name in shadows or tbl_name in shadows:
            continue
        if name in exclude or tbl_name in exclude:
            continue
        out.objects[name] = SchemaObj(type_, name, tbl_name, sql, name in virtual)
    return out


# --------------------------------------------------------------------------- table info


@dataclass
class TableInfo:
    name: str
    columns: list[str]  # insertable columns (no generated / hidden)
    key: list[str]  # primary-key columns, or ["rowid"]
    uses_rowid: bool  # key is the implicit rowid (must be written explicitly)
    key_not_null: bool  # key can be matched with "=" (faster joins)
    unique_cols: set[str]  # columns covered by non-PK unique indexes
    unique_expr: bool  # a unique index uses an expression


def table_info(conn: sqlite3.Connection, schema: str, name: str, virtual: bool) -> TableInfo:
    if virtual:
        cols = [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({lit(name)})")]
        return TableInfo(name, cols, ["rowid"], True, True, set(), False)

    xinfo = conn.execute(f"PRAGMA {schema}.table_xinfo({lit(name)})").fetchall()
    # (cid, name, type, notnull, dflt, pk, hidden); hidden 2/3 = generated column
    cols = [r[1] for r in xinfo if r[6] == 0]
    pk = sorted((r[5], r[1], r[2], r[3]) for r in xinfo if r[5] > 0)
    if pk:
        key = [p[1] for p in pk]
        int_pk = len(pk) == 1 and pk[0][2].upper() == "INTEGER"
        without_rowid = bool(conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=? "
            "AND sql LIKE '%WITHOUT%ROWID%'", (name,)
        ).fetchone())
        key_not_null = int_pk or without_rowid or all(p[3] for p in pk)
        uses_rowid = False
    else:
        key, uses_rowid, key_not_null = ["rowid"], True, True

    unique_cols: set[str] = set()
    unique_expr = False
    for idx in conn.execute(f"PRAGMA {schema}.index_list({lit(name)})").fetchall():
        # (seq, name, unique, origin, partial)
        if not idx[2] or idx[3] == "pk":
            continue
        for _, cid, cname in conn.execute(f"PRAGMA {schema}.index_info({lit(idx[1])})"):
            if cid == -2:
                unique_expr = True
            elif cname:
                unique_cols.add(cname)
    return TableInfo(name, cols, key, uses_rowid, key_not_null, unique_cols, unique_expr)


# --------------------------------------------------------------------------- data diff


@dataclass
class Stats:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    tables_changed: set[str] = field(default_factory=set)

    @property
    def statements(self) -> int:
        return self.inserted + self.updated + self.deleted


def _insert_sql(t: TableInfo, cols: list[str], values) -> str:
    return (
        f"INSERT INTO {qi(t.name)} ({', '.join(qi(c) for c in cols)}) "
        f"VALUES ({', '.join(lit(v) for v in values)});\n"
    )


def _write_cols(t: TableInfo) -> list[str]:
    return (["rowid"] if t.uses_rowid else []) + t.columns


def dump_table(conn: sqlite3.Connection, t: TableInfo, out: TextIO, stats: Stats) -> None:
    """Insert every row of NEW.t (table was just created or rebuilt)."""
    cols = _write_cols(t)
    sel = ", ".join(f"n.{qi(c)}" for c in cols)
    for row in conn.execute(f"SELECT {sel} FROM {NEW}.{qi(t.name)} n ORDER BY {', '.join('n.' + qi(k) for k in t.key)}"):
        out.write(_insert_sql(t, cols, row))
        stats.inserted += 1
        stats.tables_changed.add(t.name)


def diff_table(conn: sqlite3.Connection, t: TableInfo, out: TextIO, stats: Stats, virtual: bool) -> None:
    tq = qi(t.name)
    op = "=" if t.key_not_null else "IS"
    join = " AND ".join(f"o.{qi(k)} {op} n.{qi(k)}" for k in t.key)
    order_n = ", ".join(f"n.{qi(k)}" for k in t.key)
    order_o = ", ".join(f"o.{qi(k)}" for k in t.key)
    cols = _write_cols(t)
    value_cols = [c for c in t.columns if c not in t.key]

    deletes: list[str] = []
    updates: list[str] = []
    inserts: list[str] = []

    # Rows only in ORIGINAL → DELETE
    sel_k = ", ".join(f"o.{qi(k)}" for k in t.key)
    for krow in conn.execute(
        f"SELECT {sel_k} FROM {ORIG}.{tq} o WHERE NOT EXISTS "
        f"(SELECT 1 FROM {NEW}.{tq} n WHERE {join}) ORDER BY {order_o}"
    ):
        deletes.append(f"DELETE FROM {tq} WHERE {key_match(t.key, krow)};\n")

    # Rows in both with different values → UPDATE (or DELETE + INSERT)
    if value_cols:
        flags = [
            f"(o.{qi(c)} IS NOT n.{qi(c)} OR typeof(o.{qi(c)}) <> typeof(n.{qi(c)}))"
            for c in value_cols
        ]
        sel = ", ".join([f"n.{qi(c)}" for c in cols] + flags)
        for row in conn.execute(
            f"SELECT {sel} FROM {NEW}.{tq} n JOIN {ORIG}.{tq} o ON {join} "
            f"WHERE {' OR '.join(flags)} ORDER BY {order_n}"
        ):
            values = dict(zip(cols, row[: len(cols)]))
            changed = [c for c, f in zip(value_cols, row[len(cols):]) if f]
            kvals = [values[k] for k in t.key]
            where = key_match(t.key, kvals)
            # Virtual tables and changes to unique columns: delete + re-insert, so
            # swapped unique values can't collide halfway through the patch.
            if virtual or t.unique_expr or t.unique_cols.intersection(changed):
                deletes.append(f"DELETE FROM {tq} WHERE {where};\n")
                inserts.append(_insert_sql(t, cols, [values[c] for c in cols]))
            else:
                sets = ", ".join(f"{qi(c)} = {lit(values[c])}" for c in changed)
                updates.append(f"UPDATE {tq} SET {sets} WHERE {where};\n")

    # Rows only in NEW → INSERT
    sel = ", ".join(f"n.{qi(c)}" for c in cols)
    for row in conn.execute(
        f"SELECT {sel} FROM {NEW}.{tq} n WHERE NOT EXISTS "
        f"(SELECT 1 FROM {ORIG}.{tq} o WHERE {join}) ORDER BY {order_n}"
    ):
        inserts.append(_insert_sql(t, cols, row))

    if deletes or updates or inserts:
        out.write(f"\n-- {t.name}: {len(deletes)} delete, {len(updates)} update, {len(inserts)} insert\n")
        out.writelines(deletes)
        out.writelines(updates)
        out.writelines(inserts)
        stats.deleted += len(deletes)
        stats.updated += len(updates)
        stats.inserted += len(inserts)
        stats.tables_changed.add(t.name)


def _seq(conn: sqlite3.Connection, schema: str) -> dict[str, int]:
    exists = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE name = 'sqlite_sequence'"
    ).fetchone()
    if not exists:
        return {}
    return dict(conn.execute(f"SELECT name, seq FROM {schema}.sqlite_sequence"))


# --------------------------------------------------------------------------- main diff


def generate_patch(original: Path, new: Path, out: TextIO, exclude: set[str]) -> Stats:
    conn = sqlite3.connect(f"file:{original}?mode=ro", uri=True)
    try:
        conn.execute(f"ATTACH DATABASE ? AS {NEW}", (f"file:{new}?mode=ro",))
        return _generate(conn, out, exclude, original, new)
    finally:
        conn.close()


def _generate(conn: sqlite3.Connection, out: TextIO, exclude: set[str], original: Path, new: Path) -> Stats:
    so = read_schema(conn, ORIG, exclude)
    sn = read_schema(conn, NEW, exclude)
    stats = Stats()

    t_o, t_n = so.of_type("table"), sn.of_type("table")
    removed_tables = [n for n in t_o if n not in t_n]
    added_tables = [n for n in t_n if n not in t_o]
    rebuilt_tables = [n for n in t_n if n in t_o and t_o[n].sql != t_n[n].sql]
    gone = set(removed_tables) | set(rebuilt_tables)
    fresh = set(added_tables) | set(rebuilt_tables)

    def changed_objs(type_: str) -> tuple[list[str], list[str]]:
        o, n = so.of_type(type_), sn.of_type(type_)
        drop = [k for k in o if (k not in n or n[k].sql != o[k].sql) and o[k].tbl_name not in gone]
        create = [k for k in n if k not in o or o[k].sql != n[k].sql or n[k].tbl_name in fresh]
        return drop, create

    drop_idx, create_idx = changed_objs("index")
    drop_view, create_view = changed_objs("view")
    drop_trg, create_trg = changed_objs("trigger")

    # Data phase first (into a temp file) so we know whether rows change.
    data = tempfile.TemporaryFile("w+", encoding="utf-8")
    try:
        for name, obj in sn.of_type("table").items():
            t = table_info(conn, NEW, name, obj.virtual)
            if name in fresh:
                data.write(f"\n-- {name}: all rows\n")
                dump_table(conn, t, data, stats)
            else:
                diff_table(conn, t, data, stats, obj.virtual)

        # Unchanged triggers would fire while the patch edits rows: drop and
        # recreate them around the data changes.
        if stats.statements:
            trg_o, trg_n = so.of_type("trigger"), sn.of_type("trigger")
            for k, o in trg_o.items():
                if k not in drop_trg and o.tbl_name not in gone:
                    drop_trg.append(k)
            for k in trg_n:
                if k not in create_trg:
                    create_trg.append(k)

        seq_o, seq_n = _seq(conn, ORIG), _seq(conn, NEW)
        seq_sql: list[str] = []
        for name, seq in seq_n.items():
            if name in exclude:
                continue
            if name in fresh or seq_o.get(name) != seq:
                seq_sql.append(f"DELETE FROM sqlite_sequence WHERE name = {lit(name)};\n")
                seq_sql.append(f"INSERT INTO sqlite_sequence (name, seq) VALUES ({lit(name)}, {lit(seq)});\n")
        for name in seq_o:
            if name not in seq_n and name not in gone and name not in exclude:
                seq_sql.append(f"DELETE FROM sqlite_sequence WHERE name = {lit(name)};\n")

        uv_o = conn.execute(f"PRAGMA {ORIG}.user_version").fetchone()[0]
        uv_n = conn.execute(f"PRAGMA {NEW}.user_version").fetchone()[0]

        schema_changes = (
            removed_tables or added_tables or rebuilt_tables or drop_idx or create_idx
            or drop_view or create_view or drop_trg or create_trg
        )

        out.write("-- SQL patch generated by vidya_sql_diff.py\n")
        out.write(f"-- original: {original.name}\n-- new:      {new.name}\n")
        if exclude:
            out.write(f"-- excluded: {', '.join(sorted(exclude))}\n")
        out.write(
            f"-- rows: {stats.inserted} insert, {stats.updated} update, {stats.deleted} delete"
            f" in {len(stats.tables_changed)} table(s)\n"
        )
        if not (schema_changes or stats.statements or seq_sql or uv_o != uv_n):
            out.write("-- files are identical: nothing to do\n")
            return stats

        out.write("\nPRAGMA foreign_keys = OFF;\nBEGIN;\n")

        if drop_trg or drop_view or drop_idx or gone:
            out.write("\n-- drop changed / removed schema objects\n")
        for k in drop_trg:
            out.write(f"DROP TRIGGER IF EXISTS {qi(k)};\n")
        for k in drop_view:
            out.write(f"DROP VIEW IF EXISTS {qi(k)};\n")
        for k in drop_idx:
            out.write(f"DROP INDEX IF EXISTS {qi(k)};\n")
        for k in removed_tables + rebuilt_tables:
            note = "  -- definition changed: rebuilt" if k in rebuilt_tables else ""
            out.write(f"DROP TABLE IF EXISTS {qi(k)};{note}\n")

        new_tables = [k for k in t_n if k in fresh]
        if new_tables:
            out.write("\n-- create tables\n")
        for k in new_tables:
            out.write(t_n[k].sql.rstrip().rstrip(";") + ";\n")

        data.seek(0)
        shutil.copyfileobj(data, out)

        if seq_sql:
            out.write("\n-- AUTOINCREMENT counters\n")
            out.writelines(seq_sql)

        objs = sn.objects
        tail = [k for k in create_idx] + [k for k in create_view] + [k for k in create_trg]
        if tail:
            out.write("\n-- create indexes / views / triggers\n")
        for k in sorted(tail, key=list(objs).index):
            out.write(objs[k].sql.rstrip().rstrip(";") + ";\n")

        if uv_o != uv_n:
            out.write(f"\nPRAGMA user_version = {int(uv_n)};\n")

        out.write("\nCOMMIT;\nPRAGMA foreign_keys = ON;\n")
        return stats
    finally:
        data.close()


# --------------------------------------------------------------------------- verify


def verify(original: Path, new: Path, patch_sql: str, exclude: set[str]) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "patched.vidya"
        src = sqlite3.connect(f"file:{original}?mode=ro", uri=True)
        dst = sqlite3.connect(work)
        try:
            src.backup(dst)
        finally:
            src.close()
        try:
            dst.executescript(patch_sql)
        except sqlite3.DatabaseError as e:
            return False, f"patch failed to apply: {e}"
        finally:
            dst.close()
        buf = io.StringIO()
        generate_patch(work, new, buf, exclude)
        if "nothing to do" not in buf.getvalue():
            rest = [l for l in buf.getvalue().splitlines() if l and not l.startswith("--")]
            return False, "differences remain after applying patch:\n  " + "\n  ".join(rest[:20])
    return True, "ok: patched original matches new"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write an SQL patch that turns ORIGINAL into NEW (both .vidya / SQLite files)."
    )
    parser.add_argument("original", type=Path, help="the .vidya file the patch will be applied to")
    parser.add_argument("new", type=Path, help="the .vidya file it should become")
    parser.add_argument("-o", "--output", type=Path, help="write the patch here (default: stdout)")
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="TABLE",
        help="leave this table out of the patch (repeatable; e.g. --exclude search_index)",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="apply the patch to a temporary copy of ORIGINAL and check it matches NEW",
    )
    args = parser.parse_args()

    for p in (args.original, args.new):
        if not p.is_file():
            print(f"error: file not found: {p}", file=sys.stderr)
            return 1
    if args.output and args.output.resolve() in (args.original.resolve(), args.new.resolve()):
        print("error: output must not be one of the input files", file=sys.stderr)
        return 1
    exclude = set(args.exclude)

    try:
        if args.verify or not args.output:
            buf = io.StringIO()
            stats = generate_patch(args.original, args.new, buf, exclude)
            text = buf.getvalue()
            if args.output:
                args.output.write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
        else:
            with args.output.open("w", encoding="utf-8") as fh:
                stats = generate_patch(args.original, args.new, fh, exclude)
    except sqlite3.DatabaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(
        f"rows: {stats.inserted} insert, {stats.updated} update, {stats.deleted} delete "
        f"in {len(stats.tables_changed)} table(s)"
        + (f"\npatch: {args.output}" if args.output else ""),
        file=sys.stderr,
    )

    if args.verify:
        ok, msg = verify(args.original, args.new, text, exclude)
        print(msg, file=sys.stderr)
        return 0 if ok else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

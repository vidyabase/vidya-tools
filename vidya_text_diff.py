#!/usr/bin/env python3
"""
Show what changed between two .vidya (SQLite) files, in readable form.

    python3 vidya_text_diff.py old.vidya new.vidya
    python3 vidya_text_diff.py old.vidya new.vidya --table records -o changes.txt
    python3 vidya_text_diff.py old.vidya new.vidya --html -o changes.html

Where vidya_sql_diff.py writes SQL to apply, this script is for people to read:
for every changed row it shows each changed column as old → new, with a
word-level diff of text, e.g.

    ~ records 5  /bg/2/13 …  (paragraph)
      content: The soul is [-never-]{+not+} born ...

With --html it writes one self-contained HTML page instead: removed text in
red, added text in green, with a filter box and collapsible tables. It opens in
any browser and needs no internet connection.

Rows are matched by primary key (rowid for tables without one). Each row is
labelled with its place in the library (the nearest url_path above it in
content_tree), so you can find it in the reader.

FTS tables (content_fts) repeat the text of records, so they are skipped
unless --include-fts is given. BLOB columns (covers, images) and other long
values without spaces (base64) are only reported as changed, with their sizes.

Neither input file is modified. Exit code: 0 = same, 1 = different, 2 = error.
"""

from __future__ import annotations

import argparse
import difflib
import html
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from vidya_sql_diff import NEW, ORIG, qi, read_schema, table_info

# --------------------------------------------------------------------------- diff model
#
# The diff is computed once into these objects; TextRenderer and HtmlRenderer
# turn them into output.
#
# Inline segments (Field.segs):
#   ("eq", text)                 unchanged
#   ("gap", head, hidden, tail)  unchanged, with `hidden` left out around changes
#   ("del", text) / ("ins", text)


@dataclass
class Field:
    col: str
    kind: str  # inline | pair | arrow | lines | value
    segs: list[tuple] = field(default_factory=list)  # inline
    old: str = ""  # pair / arrow
    new: str = ""
    lines: list[tuple[str, str]] = field(default_factory=list)  # lines: (" "|"-"|"+", text)
    value: str = ""  # value (added / removed rows)


@dataclass
class Change:
    sign: str  # ~ changed, + added, - removed
    table: str
    key: str
    where: str
    label: str
    fields: list[Field]


@dataclass
class TableResult:
    name: str = ""
    changed: int = 0
    added: int = 0
    removed: int = 0
    changes: list[Change] = field(default_factory=list)
    limited: bool = False

    @property
    def any(self) -> bool:
        return bool(self.changed or self.added or self.removed)


# --------------------------------------------------------------------------- values

BLOB = (bytes, bytearray, memoryview)


def opaque(v) -> str | None:
    """Short description for values not worth showing as text (BLOBs, base64)."""
    if isinstance(v, BLOB):
        return f"<blob {len(bytes(v))} bytes>"
    if isinstance(v, str) and len(v) > 300 and not any(c.isspace() for c in v[:2000]):
        return f"<text {len(v)} chars>"
    return None


def show_value(v, width: int = 0) -> str:
    if v is None:
        return "NULL"
    o = opaque(v)
    if o:
        return o
    s = str(v)
    if width and len(s) > width:
        s = s[: width - 1] + "…"
    return s


TOKEN = re.compile(r"\s+|<[^>]*>|\w+|[^\w\s]", re.UNICODE)


def word_diff(old: str, new: str, context: int, min_similarity: float) -> list[tuple] | None:
    """Inline diff segments, or None if the texts are too different to mark inline."""
    a, b = TOKEN.findall(old), TOKEN.findall(new)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if min_similarity and sm.ratio() < min_similarity:
        return None  # rewritten rather than edited: inline marks would be noise
    ops = sm.get_opcodes()
    segs: list[tuple] = []
    for i, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            run = a[i1:i2]
            # context counts words only, so whitespace and tags don't use it up
            keep_head = 0 if i == 0 else context
            keep_tail = 0 if i == len(ops) - 1 else context
            n_words = sum(1 for t in run if not t.isspace())
            if context < 0 or n_words <= keep_head + keep_tail + 2:
                segs.append(("eq", "".join(run)))
            else:
                h = _take_words(run, keep_head)
                t = _take_words(run[::-1], keep_tail)
                segs.append(("gap", "".join(run[:h]), "".join(run[h:len(run) - t]), "".join(run[len(run) - t:])))
        else:
            if i2 > i1:
                segs.append(("del", "".join(a[i1:i2])))
            if j2 > j1:
                segs.append(("ins", "".join(b[j1:j2])))
    return segs


def _take_words(run: list[str], n: int) -> int:
    """Number of tokens from the start of `run` that hold `n` words."""
    count = words = 0
    for t in run:
        if words >= n:
            break
        count += 1
        if not t.isspace():
            words += 1
    return count


def line_diff(old: str, new: str) -> list[tuple[str, str]]:
    out = []
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line[:2] in ("- ", "+ ", "  "):
            out.append((line[0], line[2:]))
    return out


def change_field(col: str, ov, nv, args) -> Field:
    o_op, n_op = (None if v is None else opaque(v) for v in (ov, nv))
    if isinstance(ov, str) and isinstance(nv, str) and not (o_op or n_op) and not args.no_word_diff:
        if args.lines:
            return Field(col, "lines", lines=line_diff(ov, nv))
        segs = word_diff(ov, nv, args.context, args.min_similarity)
        if segs is not None:
            return Field(col, "inline", segs=segs)
        if max(len(ov), len(nv)) <= 60 and "\n" not in ov + nv:
            return Field(col, "arrow", old=ov, new=nv)
        return Field(col, "pair", old=ov, new=nv)
    o, n = show_value(ov), show_value(nv)
    if type(ov) is not type(nv) and ov is not None and nv is not None and not (o_op or n_op):
        o += f" ({type(ov).__name__})"
        n += f" ({type(nv).__name__})"
    kind = "pair" if max(len(o), len(n)) > 60 or "\n" in o + n else "arrow"
    return Field(col, kind, old=o, new=n)


# --------------------------------------------------------------------------- locations


class Locator:
    """Finds where an item sits in the library: the url_path of the item or of
    its nearest ancestor. content_tree.item_id is unique across all entity
    tables and parent_id points at the parent's item_id."""

    NO_LOCATION = {"url_paths", "db_meta", "quick_nav", "image_assets", "sqlite_sequence"}

    def __init__(self, conn: sqlite3.Connection, schema: str):
        self.conn, self.schema = conn, schema
        self._parent: dict[int, int | None] | None = None
        self._url: dict[int, str] = {}
        self._tree_item: dict[int, int] = {}  # content_tree.id -> item_id

    def _load(self) -> None:
        self._parent = {}
        s = self.schema
        names = {r[0] for r in self.conn.execute(f"SELECT name FROM {s}.sqlite_master WHERE type='table'")}
        if "content_tree" in names:
            for cid, item_id, parent_id in self.conn.execute(
                f"SELECT id, item_id, parent_id FROM {s}.content_tree"
            ):
                self._parent[item_id] = parent_id
                self._tree_item[cid] = item_id
        if "url_paths" in names:
            for path, item_id in self.conn.execute(f"SELECT url_path, item_id FROM {s}.url_paths"):
                # keep the shortest path if an item has several
                if item_id not in self._url or len(path) < len(self._url[item_id]):
                    self._url[item_id] = path

    def where(self, table: str, key: tuple) -> str:
        if len(key) != 1 or not isinstance(key[0], int) or table in self.NO_LOCATION:
            return ""
        if self._parent is None:
            self._load()
        item = key[0]
        if table == "content_tree":
            item = self._tree_item.get(item)
        seen = set()
        steps = 0
        while item is not None and item not in seen:
            seen.add(item)
            if item in self._url:
                return self._url[item] + (" …" if steps else "")
            if item not in self._parent:
                return ""
            item = self._parent[item]
            steps += 1
        return ""


# --------------------------------------------------------------------------- diff


LABEL_COLS = ("content_type", "type", "title", "key", "url_path")


def diff_table(conn, name: str, virtual: bool, args, loc_o: Locator, loc_n: Locator) -> TableResult:
    t = table_info(conn, NEW, name, virtual)
    t_o = table_info(conn, ORIG, name, virtual)
    common = [c for c in t.columns if c in t_o.columns]
    tq = qi(name)
    op = "=" if t.key_not_null else "IS"
    nk = len(t.key)
    value_cols = [c for c in common if c not in t.key]
    keys = ", ".join(f"{{a}}.{qi(k)}" for k in t.key)
    res = TableResult(name)
    limit = args.max_rows

    def label(row: dict) -> str:
        for c in LABEL_COLS:
            if c in row and c not in t.key and isinstance(row[c], str) and row[c]:
                return show_value(row[c], 40)
        return ""

    def change(sign: str, krow: tuple, row: dict, locator: Locator, fields: list[Field]) -> Change:
        where = locator.where(name, krow) if args.locate else ""
        return Change(sign, name, ", ".join(show_value(v) for v in krow), where, label(row), fields)

    # changed rows
    if value_cols:
        join = " AND ".join(f"o.{qi(k)} {op} n.{qi(k)}" for k in t.key)
        flags = [
            f"(o.{qi(c)} IS NOT n.{qi(c)} OR typeof(o.{qi(c)}) <> typeof(n.{qi(c)}))"
            for c in value_cols
        ]
        sel = ", ".join(
            [keys.format(a="n")] + [f"o.{qi(c)}" for c in value_cols] + [f"n.{qi(c)}" for c in value_cols]
        )
        nv = len(value_cols)
        for row in conn.execute(
            f"SELECT {sel} FROM {NEW}.{tq} n JOIN {ORIG}.{tq} o ON {join} "
            f"WHERE {' OR '.join(flags)} ORDER BY " + ", ".join(f"n.{qi(k)}" for k in t.key)
        ):
            res.changed += 1
            if limit and res.changed > limit:
                res.limited = True
                continue
            krow = tuple(row[:nk])
            old = dict(zip(value_cols, row[nk:nk + nv]))
            new = dict(zip(value_cols, row[nk + nv:]))
            fields = [
                change_field(c, old[c], new[c], args)
                for c in value_cols
                if not (old[c] == new[c] and type(old[c]) is type(new[c]))
            ]
            res.changes.append(change("~", krow, new, loc_n, fields))

    # removed / added rows
    for sign, a, other, locator, info in (("-", ORIG, NEW, loc_o, t_o), ("+", NEW, ORIG, loc_n, t)):
        cols = [c for c in info.columns if c not in t.key]
        sel = ", ".join([keys.format(a="x")] + [f"x.{qi(c)}" for c in cols])
        match = " AND ".join(f"x.{qi(k)} {op} y.{qi(k)}" for k in t.key)
        count = 0
        for row in conn.execute(
            f"SELECT {sel} FROM {a}.{tq} x WHERE NOT EXISTS "
            f"(SELECT 1 FROM {other}.{tq} y WHERE {match}) ORDER BY "
            + ", ".join(f"x.{qi(k)}" for k in t.key)
        ):
            count += 1
            if limit and count > limit:
                res.limited = True
                continue
            krow = tuple(row[:nk])
            vals = dict(zip(cols, row[nk:]))
            fields = [Field(c, "value", value=show_value(v)) for c, v in vals.items() if v is not None]
            res.changes.append(change(sign, krow, vals, locator, fields))
        if sign == "-":
            res.removed = count
        else:
            res.added = count
    return res


# --------------------------------------------------------------------------- text output


@dataclass
class Style:
    color: bool

    def _c(self, code: str, s: str) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if self.color and s else s

    def head(self, s): return self._c("1", s)
    def table(self, s): return self._c("1;36", s)
    def dim(self, s): return self._c("2", s)
    def loc(self, s): return self._c("33", s)
    def old(self, s): return self._c("31", s)
    def new(self, s): return self._c("32", s)
    def del_(self, s): return self._c("1;31;9", s) if self.color else f"[-{s}-]"
    def ins(self, s): return self._c("1;32", s) if self.color else f"{{+{s}+}}"


class TextRenderer:
    def __init__(self, out: TextIO, color: bool, width: int):
        self.out, self.st, self.width = out, Style(color), width

    def cut(self, s: str) -> str:
        return show_value(s, self.width).replace("\n", "\\n")

    def start(self, old: Path, new: Path) -> None:
        self.out.write(self.st.head(f"--- {old}\n+++ {new}\n\n"))

    def schema(self, items: list[tuple[str, str, list[tuple[str, str]]]]) -> None:
        st = self.st
        lines = []
        for sign, text, diff in items:
            paint = {"-": st.old, "+": st.new}.get(sign, str)
            lines.append(paint(f"{sign} {text}"))
            for op, l in diff:
                if op != " ":
                    lines.append("    " + (st.old if op == "-" else st.new)(f"{op} {l}"))
        self.out.write(st.table("=== schema") + "\n\n" + "\n".join(lines) + "\n\n")

    def table(self, r: TableResult, limit: int) -> None:
        st = self.st
        summary = f"{r.changed} changed, {r.added} added, {r.removed} removed"
        self.out.write(st.table(f"=== {r.name}") + "  " + st.dim(summary) + "\n\n")
        for c in r.changes:
            paint = {"-": st.old, "+": st.new}.get(c.sign, str)
            head = [paint(c.sign), c.table, c.key]
            if c.where:
                head.append(st.loc(c.where))
            if c.label:
                head.append(st.dim(f"({c.label})"))
            lines = [" ".join(head)]
            for f in c.fields:
                lines.extend(self.field(f, paint))
            self.out.write("\n".join(lines) + "\n\n")
        if r.limited:
            self.out.write(st.dim(f"    … only the first {limit} rows of each kind shown (--max-rows)\n\n"))

    def field(self, f: Field, paint) -> list[str]:
        st = self.st
        if f.kind == "value":
            return [f"    {f.col}: " + paint(self.cut(f.value))]
        if f.kind == "arrow":
            return [f"    {f.col}: " + st.old(f.old) + " → " + st.new(f.new)]
        if f.kind == "pair":
            return [f"    {f.col}:", "      " + st.old("- " + self.cut(f.old)),
                    "      " + st.new("+ " + self.cut(f.new))]
        if f.kind == "lines":
            body = [{"-": st.old, "+": st.new}.get(op, str)(f"{op} {l}") for op, l in f.lines]
            return [f"    {f.col}:"] + ["      " + l for l in body]
        parts = []
        for s in f.segs:
            if s[0] == "eq":
                parts.append(self.long_tokens(s[1]))
            elif s[0] == "gap":
                parts.append(self.long_tokens(s[1]) + st.dim("…") + self.long_tokens(s[3]))
            elif s[0] == "del":
                parts.append(st.del_(self.long_tokens(s[1])))
            else:
                parts.append(st.ins(self.long_tokens(s[1])))
        body = "".join(parts).replace("\r", "").split("\n")
        if len(body) == 1:
            return [f"    {f.col}: {body[0]}"]
        return [f"    {f.col}:"] + ["      " + l for l in body]

    def long_tokens(self, s: str) -> str:
        """Cut single tokens longer than --width (e.g. long URLs)."""
        w = self.width
        if not w or len(s) <= w:
            return s
        return re.sub(r"\S{%d,}" % (w + 1), lambda m: m.group()[: w // 2] + f"…({len(m.group())} chars)", s)

    def finish(self, any_diff: bool, summary: str) -> None:
        if not any_diff:
            self.out.write("no differences\n")


# --------------------------------------------------------------------------- HTML output

HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vidya diff</title>
<style>
:root {
  --bg: #fbfbfa; --panel: #ffffff; --fg: #1f2328; --muted: #6a737d; --line: #e3e3e0;
  --del-bg: #ffe2e0; --del-fg: #a40e26; --ins-bg: #d6f5dd; --ins-fg: #11652c;
  --chg: #9a6700; --loc: #8250df; --tag: #8c959f; --accent: #0969da;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f1115; --panel: #171a21; --fg: #e6e6e6; --muted: #8b949e; --line: #2a2f39;
    --del-bg: #4a1d22; --del-fg: #ffb3ad; --ins-bg: #173d24; --ins-fg: #9be9a8;
    --chg: #d4a72c; --loc: #c3a6ff; --tag: #6e7681; --accent: #58a6ff;
  }
}
:root[data-theme="dark"] {
  --bg: #0f1115; --panel: #171a21; --fg: #e6e6e6; --muted: #8b949e; --line: #2a2f39;
  --del-bg: #4a1d22; --del-fg: #ffb3ad; --ins-bg: #173d24; --ins-fg: #9be9a8;
  --chg: #d4a72c; --loc: #c3a6ff; --tag: #6e7681; --accent: #58a6ff;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans", sans-serif; }
header { position: sticky; top: 0; z-index: 2; background: var(--panel);
  border-bottom: 1px solid var(--line); padding: 12px 16px; }
h1 { font-size: 17px; margin: 0 0 2px; }
.files { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
.files .o { color: var(--del-fg); } .files .n { color: var(--ins-fg); }
.bar { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin-top: 10px; }
.bar input[type=search] { flex: 1 1 240px; min-width: 0; padding: 6px 10px; font: inherit;
  border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--fg); }
.bar label { font-size: 13px; white-space: nowrap; cursor: pointer; }
.count { font-size: 13px; color: var(--muted); }
main { max-width: 1200px; margin: 0 auto; padding: 16px; }
details.tbl { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; margin-bottom: 16px; }
details.tbl > summary { cursor: pointer; padding: 10px 14px; font-weight: 600; list-style-position: inside; }
details.tbl > summary .n { font-weight: 400; color: var(--muted); font-size: 13px; margin-left: 8px; }
.row { border-top: 1px solid var(--line); padding: 10px 14px; }
.rh { font-size: 13px; color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: baseline; }
.sign { font-weight: 700; font-family: ui-monospace, Menlo, monospace; }
.k-chg .sign { color: var(--chg); } .k-add .sign { color: var(--ins-fg); } .k-del .sign { color: var(--del-fg); }
.key { font-family: ui-monospace, Menlo, monospace; color: var(--fg); }
.loc { color: var(--loc); font-weight: 600; }
.f { display: grid; grid-template-columns: minmax(80px, max-content) 1fr; gap: 2px 12px; margin-top: 6px; }
.col { color: var(--muted); font-size: 13px; font-family: ui-monospace, Menlo, monospace; padding-top: 1px; }
.v { white-space: pre-wrap; overflow-wrap: anywhere; min-width: 0; }
del { background: var(--del-bg); color: var(--del-fg); text-decoration: line-through; border-radius: 3px; padding: 0 1px; }
ins { background: var(--ins-bg); color: var(--ins-fg); text-decoration: none; border-radius: 3px; padding: 0 1px; }
.t { color: var(--tag); }
del .t, ins .t { color: inherit; opacity: .7; }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.pair > div, .val-del, .val-add { border-radius: 6px; padding: 4px 8px; white-space: pre-wrap; overflow-wrap: anywhere; }
.val-del, .val-add { display: inline-block; max-width: 100%; padding: 0 6px; }
.pair .o, .val-del { background: var(--del-bg); color: var(--del-fg); }
.pair .n, .val-add { background: var(--ins-bg); color: var(--ins-fg); }
.arrow .o { color: var(--del-fg); } .arrow .n { color: var(--ins-fg); }
.arrow .to { color: var(--muted); margin: 0 6px; }
.lines div { white-space: pre-wrap; overflow-wrap: anywhere; font-family: ui-monospace, Menlo, monospace; font-size: 13px; }
.lines .lo { background: var(--del-bg); color: var(--del-fg); } .lines .ln { background: var(--ins-bg); color: var(--ins-fg); }
.gap .gh { display: none; }
.gap.open .gh, body.full .gap .gh { display: inline; }
.gap.open button, body.full .gap button { display: none; }
.gap button { font: inherit; color: var(--accent); background: none; border: 1px dashed var(--line);
  border-radius: 4px; padding: 0 4px; cursor: pointer; }
.limited, .empty { color: var(--muted); padding: 10px 14px; border-top: 1px solid var(--line); font-size: 13px; }
.schema .row div { font-family: ui-monospace, Menlo, monospace; font-size: 13px; white-space: pre-wrap; }
.hidden { display: none !important; }
@media (max-width: 640px) {
  .pair { grid-template-columns: 1fr; }
  .f { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
"""

HTML_TAIL = """
<script>
(function () {
  var q = document.getElementById('q'), rows = Array.prototype.slice.call(document.querySelectorAll('.row[data-k]'));
  var kinds = { chg: document.getElementById('k-chg'), add: document.getElementById('k-add'), del: document.getElementById('k-del') };
  var count = document.getElementById('count');
  rows.forEach(function (r) { r._t = r.textContent.toLowerCase(); });
  function apply() {
    var s = q.value.trim().toLowerCase(), shown = 0;
    rows.forEach(function (r) {
      var ok = kinds[r.dataset.k].checked && (!s || r._t.indexOf(s) !== -1);
      r.classList.toggle('hidden', !ok);
      if (ok) shown++;
    });
    document.querySelectorAll('details.tbl[data-t]').forEach(function (d) {
      d.classList.toggle('hidden', !d.querySelector('.row[data-k]:not(.hidden)'));
    });
    count.textContent = shown + ' of ' + rows.length + ' rows shown';
  }
  var timer;
  q.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(apply, 150); });
  Object.keys(kinds).forEach(function (k) { kinds[k].addEventListener('change', apply); });
  document.getElementById('full').addEventListener('change', function (e) {
    document.body.classList.toggle('full', e.target.checked);
  });
  document.addEventListener('click', function (e) {
    var b = e.target.closest('.gap button');
    if (b) b.parentNode.classList.add('open');
  });
  apply();
})();
</script>
</body>
</html>
"""


def esc_tags(s: str) -> str:
    """Escape text and dim HTML tags inside it (record content is HTML source)."""
    return re.sub(r"&lt;.*?&gt;", lambda m: f'<span class="t">{m.group()}</span>', html.escape(s, quote=False))


class HtmlRenderer:
    KIND = {"~": "chg", "+": "add", "-": "del"}

    def __init__(self, out: TextIO):
        self.out = out

    def start(self, old: Path, new: Path) -> None:
        e = html.escape
        self.out.write(HTML_HEAD)
        self.out.write(
            '<header>\n<h1>Vidya diff</h1>\n'
            f'<div class="files"><span class="o">− {e(str(old))}</span> &nbsp; '
            f'<span class="n">+ {e(str(new))}</span></div>\n'
            '<div class="bar">\n'
            '<input id="q" type="search" placeholder="Filter by path, id or text…" aria-label="Filter">\n'
            '<label><input type="checkbox" id="k-chg" checked> changed</label>\n'
            '<label><input type="checkbox" id="k-add" checked> added</label>\n'
            '<label><input type="checkbox" id="k-del" checked> removed</label>\n'
            '<label><input type="checkbox" id="full"> full text</label>\n'
            '<span class="count" id="count"></span>\n'
            '</div>\n</header>\n<main>\n'
        )

    def schema(self, items) -> None:
        e = html.escape
        self.out.write('<details class="tbl schema" open><summary>schema</summary>\n')
        for sign, text, diff in items:
            cls = {"-": "val-del", "+": "val-add"}.get(sign, "")
            self.out.write(f'<div class="row"><div class="{cls}">{e(sign)} {e(text)}</div>')
            if diff:
                self.out.write('<div class="lines">')
                for op, l in diff:
                    c = {"-": "lo", "+": "ln"}.get(op, "")
                    self.out.write(f'<div class="{c}">{e(op)} {e(l)}</div>')
                self.out.write("</div>")
            self.out.write("</div>\n")
        self.out.write("</details>\n")

    def table(self, r: TableResult, limit: int) -> None:
        e = html.escape
        w = self.out.write
        w(f'<details class="tbl" data-t="{e(r.name)}" open><summary>{e(r.name)}'
          f'<span class="n">{r.changed} changed, {r.added} added, {r.removed} removed</span></summary>\n')
        for c in r.changes:
            k = self.KIND[c.sign]
            head = [f'<span class="sign">{e(c.sign)}</span>', f'<span class="key">{e(c.key)}</span>']
            if c.where:
                head.append(f'<span class="loc">{e(c.where)}</span>')
            if c.label:
                head.append(f'<span>{e(c.label)}</span>')
            w(f'<div class="row k-{k}" data-k="{k}"><div class="rh">{"".join(head)}</div>')
            for f in c.fields:
                w(f'<div class="f"><div class="col">{e(f.col)}</div>{self.field(f, c.sign)}</div>')
            w("</div>\n")
        if r.limited:
            w(f'<div class="limited">Only the first {limit} rows of each kind are shown (--max-rows).</div>\n')
        w("</details>\n")

    def field(self, f: Field, sign: str) -> str:
        if f.kind == "value":
            cls = "val-del" if sign == "-" else "val-add"
            return f'<div class="v"><div class="{cls}">{esc_tags(f.value)}</div></div>'
        if f.kind == "arrow":
            return (f'<div class="v arrow"><span class="o">{esc_tags(f.old)}</span>'
                    f'<span class="to">→</span><span class="n">{esc_tags(f.new)}</span></div>')
        if f.kind == "pair":
            return (f'<div class="v pair"><div class="o">{esc_tags(f.old)}</div>'
                    f'<div class="n">{esc_tags(f.new)}</div></div>')
        if f.kind == "lines":
            body = "".join(
                f'<div class="{ {"-": "lo", "+": "ln"}.get(op, "") }">{html.escape(op)} {esc_tags(l)}</div>'
                for op, l in f.lines
            )
            return f'<div class="v lines">{body}</div>'
        parts = []
        for s in f.segs:
            if s[0] == "eq":
                parts.append(esc_tags(s[1]))
            elif s[0] == "gap":
                parts.append(
                    f'{esc_tags(s[1])}<span class="gap"><button type="button" title="Show hidden text">…</button>'
                    f'<span class="gh">{esc_tags(s[2])}</span></span>{esc_tags(s[3])}'
                )
            elif s[0] == "del":
                parts.append(f"<del>{esc_tags(s[1])}</del>")
            else:
                parts.append(f"<ins>{esc_tags(s[1])}</ins>")
        return f'<div class="v">{"".join(parts)}</div>'

    def finish(self, any_diff: bool, summary: str) -> None:
        if not any_diff:
            self.out.write('<div class="empty">No differences.</div>\n')
        else:
            self.out.write(f'<div class="empty">{html.escape(summary)}</div>\n')
        self.out.write("</main>\n" + HTML_TAIL)


# --------------------------------------------------------------------------- main


def run(old: Path, new: Path, renderer, args) -> int:
    conn = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
    try:
        conn.execute(f"ATTACH DATABASE ? AS {NEW}", (f"file:{new}?mode=ro",))
        exclude = set(args.exclude)
        so = read_schema(conn, ORIG, exclude)
        sn = read_schema(conn, NEW, exclude)

        renderer.start(old, new)

        # schema
        schema_items = []
        for n, o in so.objects.items():
            if n not in sn.objects:
                schema_items.append(("-", f"{o.type} {n}", []))
            elif sn.objects[n].sql != o.sql:
                schema_items.append(("~", f"{o.type} {n}: definition changed",
                                     [d for d in line_diff(o.sql, sn.objects[n].sql) if d[0] != " "]))
        for n, o in sn.objects.items():
            if n not in so.objects:
                schema_items.append(("+", f"{o.type} {n}", []))
        uv_o = conn.execute(f"PRAGMA {ORIG}.user_version").fetchone()[0]
        uv_n = conn.execute(f"PRAGMA {NEW}.user_version").fetchone()[0]
        if uv_o != uv_n:
            schema_items.append(("~", f"user_version: {uv_o} → {uv_n}", []))
        if schema_items:
            renderer.schema(schema_items)

        # data
        loc_o, loc_n = Locator(conn, ORIG), Locator(conn, NEW)
        changed = added = removed = tables_changed = 0
        for name, obj in sn.of_type("table").items():
            if name not in so.objects or so.objects[name].type != "table":
                continue  # new table: listed under schema
            if obj.virtual and not args.include_fts:
                continue
            if args.table and name not in args.table:
                continue
            r = diff_table(conn, name, obj.virtual, args, loc_o, loc_n)
            if r.any:
                renderer.table(r, args.max_rows)
                tables_changed += 1
            changed, added, removed = changed + r.changed, added + r.added, removed + r.removed

        summary = f"rows: {changed} changed, {added} added, {removed} removed in {tables_changed} table(s)"
        any_diff = bool(schema_items or tables_changed)
        renderer.finish(any_diff, summary)
        print(summary, file=sys.stderr)
        return 1 if any_diff else 0
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Show the differences between two .vidya files: old text → new text."
    )
    p.add_argument("old", type=Path, help="the older .vidya file")
    p.add_argument("new", type=Path, help="the newer .vidya file")
    p.add_argument("-o", "--output", type=Path, help="write the report here (default: stdout)")
    p.add_argument("--html", action="store_true",
                   help="write a self-contained HTML page (open it in a browser) instead of text")
    p.add_argument("--table", action="append", default=[], metavar="TABLE",
                   help="only compare this table (repeatable)")
    p.add_argument("--exclude", action="append", default=[], metavar="TABLE",
                   help="skip this table (repeatable)")
    p.add_argument("--include-fts", action="store_true",
                   help="also compare FTS search tables (they repeat the records text)")
    p.add_argument("--context", type=int, default=8, metavar="N",
                   help="words of unchanged text to keep around each change (default 8; -1 = all). "
                        "In HTML the rest can be expanded by clicking '…'")
    p.add_argument("--min-similarity", type=float, default=0.5, metavar="R",
                   help="below this similarity (0-1) show text as whole old / new "
                        "instead of inline marks (default 0.5; 0 = always inline)")
    p.add_argument("--lines", action="store_true",
                   help="show text changes as whole '- old' / '+ new' lines instead of inline")
    p.add_argument("--no-word-diff", action="store_true",
                   help="show changed text as plain 'old → new' values")
    p.add_argument("--width", type=int, default=200, metavar="N",
                   help="text output: cut long values to N characters (default 200; 0 = no limit)")
    p.add_argument("--max-rows", type=int, default=0, metavar="N",
                   help="show at most N changed / added / removed rows per table (counts stay exact)")
    p.add_argument("--no-locate", dest="locate", action="store_false",
                   help="don't look up each row's url_path")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                   help="text output: colors (default: auto = when writing to a terminal)")
    args = p.parse_args()

    for f in (args.old, args.new):
        if not f.is_file():
            print(f"error: file not found: {f}", file=sys.stderr)
            return 2
    if args.output and args.output.resolve() in (args.old.resolve(), args.new.resolve()):
        print("error: output must not be one of the input files", file=sys.stderr)
        return 2

    out = args.output.open("w", encoding="utf-8") if args.output else sys.stdout
    try:
        if args.html:
            renderer = HtmlRenderer(out)
        else:
            if args.color == "always":
                color = True
            elif args.color == "never":
                color = False
            else:
                color = not args.output and sys.stdout.isatty() and "NO_COLOR" not in os.environ
            renderer = TextRenderer(out, color, args.width)
        try:
            return run(args.old, args.new, renderer, args)
        except sqlite3.DatabaseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except BrokenPipeError:
            return 0
    finally:
        if args.output:
            out.close()
            print(f"report: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

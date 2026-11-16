"""Export .vidya subtrees to BBT importer text format."""

from __future__ import annotations

import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from vidya_lib.item_ids import connect_vidya, table_exists

# content_tree.type values: skip node and descendants
SKIP_CONTAINER_TYPES: frozenset[str] = frozenset({"footnote-container"})

# content_tree.type or records.content_type: omit block, warn once per type
SKIP_LEAF_TYPES: frozenset[str] = frozenset(
    {
        "wfw-pairs",
        "image",
        "audio",
        "video",
        "file-link",
        "devanagari",
        "bengali",
        "synonyms",
    }
)

CONTAINER_TABLE: dict[str, tuple[str, str]] = {
    "book": ("books", "book"),
    "canto": ("books", "canto"),
    "chapter": ("chapters", "chapter"),
    "verse": ("verses", "verse"),
    "section": ("sections", "section"),
    "transcript": ("transcripts", "transcript"),
    "letter": ("letters", "letter"),
}

# Vidya record content_type -> canonical BBT @tag (vedabase_editor backbone/matcher.py)
RECORD_BBT_TAG: dict[str, str] = {
    "paragraph": "para",
    "paragraph-intro": "intro",
    "paragraph-list": "para-list",
    "paragraph-poem": "poem",
    "paragraph-speech": "speech",
    "paragraph-thus-ends": "thus-ends",
    "paragraph-end": "ch-end",
    "paragraph-space": "purp-space",
    "small-header": "small-header",
    "sub-chapter": "sub-chapter",
    "h3": "h3",
    "translation": "trans",
    "verse-text": "verse-in-purp",
    "verse-in-purport": "verse-in-purp",
    "reference": "quote-ref",
    "footnote": "footnote",
    "footnote-old": "footnote",
    "quote": "quote",
    "quote-centered": "quote-center",
    "divider": "asterisk",
    "verse-number": "textnum",
    "heading": "small-header",
}

_DIGIT_CENTERED_RE = re.compile(r"^\d+$")


@dataclass
class TreeNode:
    item_id: int
    parent_id: int | None
    type: str
    sort_order: int
    children: list[TreeNode] = field(default_factory=list)


def normalize_url_path(path: str) -> str:
    p = path.strip()
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/")


def resolve_root_item_id(conn: sqlite3.Connection, parent: str) -> int:
    parent = parent.strip()
    if parent.isdigit():
        item_id = int(parent)
        row = conn.execute(
            "SELECT 1 FROM content_tree WHERE item_id = ?", (item_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"No content_tree row for item_id {item_id}")
        return item_id

    url_path = normalize_url_path(parent)
    row = conn.execute(
        "SELECT item_id FROM url_paths WHERE url_path = ?", (url_path,)
    ).fetchone()
    if row:
        return int(row[0])

    alt = url_path.lstrip("/")
    row = conn.execute(
        "SELECT item_id FROM url_paths WHERE url_path = ? OR url_path = ?",
        (alt, "/" + alt),
    ).fetchone()
    if row:
        return int(row[0])

    raise ValueError(f"Unknown url_path or item id: {parent}")


def _load_url_path_codes(
    conn: sqlite3.Connection, item_ids: set[int]
) -> dict[int, str]:
    if not item_ids or not table_exists(conn, "url_paths"):
        return {}
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT item_id, url_path FROM url_paths WHERE item_id IN ({placeholders})",
        tuple(item_ids),
    )
    codes: dict[int, str] = {}
    for item_id, url_path in rows:
        segment = (url_path or "").rstrip("/").split("/")[-1]
        if segment:
            codes[int(item_id)] = segment
    return codes


def _load_subtree(conn: sqlite3.Connection, root_id: int) -> dict[int, TreeNode]:
    rows = conn.execute(
        """
        WITH RECURSIVE sub(item_id) AS (
            SELECT ?
            UNION ALL
            SELECT ct.item_id FROM content_tree ct
            INNER JOIN sub ON ct.parent_id = sub.item_id
        )
        SELECT ct.item_id, ct.parent_id, ct.type, ct.sort_order
        FROM content_tree ct
        INNER JOIN sub ON ct.item_id = sub.item_id
        """,
        (root_id,),
    ).fetchall()

    nodes: dict[int, TreeNode] = {}
    for item_id, parent_id, typ, sort_order in rows:
        nodes[item_id] = TreeNode(
            item_id=int(item_id),
            parent_id=int(parent_id) if parent_id is not None else None,
            type=str(typ),
            sort_order=int(sort_order or 0),
        )

    for node in nodes.values():
        if node.parent_id is not None and node.parent_id in nodes:
            nodes[node.parent_id].children.append(node)

    for node in nodes.values():
        node.children.sort(key=lambda n: n.sort_order)

    return nodes


def _fetch_book(conn: sqlite3.Connection, item_id: int) -> tuple[str, str]:
    row = conn.execute(
        "SELECT title, type FROM books WHERE id = ?", (item_id,)
    ).fetchone()
    if not row:
        return ("", "book")
    title = row[0] or ""
    kind = row[1] or "book"
    return (title, kind)


def _fetch_chapter(
    conn: sqlite3.Connection, item_id: int
) -> tuple[str, str | None]:
    row = conn.execute(
        "SELECT title, chapter_title FROM chapters WHERE id = ?", (item_id,)
    ).fetchone()
    if not row:
        return ("", None)
    return (row[0] or "", row[1] or None)


def _fetch_simple_title(conn: sqlite3.Connection, table: str, item_id: int) -> str:
    if not table_exists(conn, table):
        return ""
    row = conn.execute(f"SELECT title FROM {table} WHERE id = ?", (item_id,)).fetchone()
    return (row[0] or "") if row else ""


def _fetch_record(conn: sqlite3.Connection, item_id: int) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT content_type, content FROM records WHERE id = ?", (item_id,)
    ).fetchone()
    if not row:
        return None
    return (str(row[0]), row[1] if row[1] is not None else "")


def _format_ch_title(chapter_title: str) -> str:
    t = chapter_title.strip()
    if not t:
        return ""
    if "<" in t:
        return t
    return f"<b>{t}</b>"


def _record_bbt_tag(content_type: str, content: str) -> str | None:
    if content_type in SKIP_LEAF_TYPES:
        return None
    if content_type == "paragraph-centered":
        text = content.strip()
        if _DIGIT_CENTERED_RE.match(text):
            return "quote-center"
        return "para-centered"
    if content_type in RECORD_BBT_TAG:
        return RECORD_BBT_TAG[content_type]
    return None


def _write_line(out: TextIO, tag: str, text: str) -> None:
    out.write(f"@{tag} = {text}\n")


class BbtExporter:
    def __init__(self, conn: sqlite3.Connection, root_id: int) -> None:
        self.conn = conn
        self.nodes = _load_subtree(conn, root_id)
        if root_id not in self.nodes:
            raise ValueError(f"Root item_id {root_id} not in library")
        self.root_id = root_id
        self.book_title: str | None = None
        self._export_root_is_book = False
        self._first_chapter_emitted = False
        self._warned_types: set[str] = set()
        self._url_codes = _load_url_path_codes(conn, set(self.nodes.keys()))

    def _warn_skip(self, typ: str, item_id: int) -> None:
        if typ not in self._warned_types:
            print(
                f"Warning: skipping type '{typ}' (e.g. item_id {item_id}); "
                f"omitting from export.",
                file=sys.stderr,
            )
            self._warned_types.add(typ)

    def _partial_code(self, node: TreeNode) -> str:
        return self._url_codes.get(node.item_id, "")

    def _emit_container(self, out: TextIO, node: TreeNode) -> None:
        slug = self._partial_code(node)
        if node.type in ("book", "canto"):
            title, kind = _fetch_book(self.conn, node.item_id)
            tag = "canto" if kind == "canto" else "book"
            _write_line(out, tag, title)
            if slug:
                _write_line(out, "code", slug)
            out.write("\n")
            self.book_title = title
            return

        if node.type == "chapter":
            title, chapter_title = _fetch_chapter(self.conn, node.item_id)
            _write_line(out, "chapter", title)
            if slug:
                _write_line(out, "code", slug)
            out.write("\n")
            if (
                self._export_root_is_book
                and not self._first_chapter_emitted
                and self.book_title
            ):
                _write_line(out, "book-title", self.book_title)
            self._first_chapter_emitted = True
            if chapter_title:
                _write_line(out, "ch-title", _format_ch_title(chapter_title))
            out.write("\n")
            return

        if node.type == "verse":
            title = _fetch_simple_title(self.conn, "verses", node.item_id)
            _write_line(out, "text", title)
            if slug:
                _write_line(out, "code", slug)
            out.write("\n")
            return

        if node.type == "section":
            title = _fetch_simple_title(self.conn, "sections", node.item_id)
            _write_line(out, "sub-chapter", title)
            if slug:
                _write_line(out, "code", slug)
            out.write("\n")
            return

        if node.type in ("transcript", "letter"):
            table = CONTAINER_TABLE[node.type][0]
            title = _fetch_simple_title(self.conn, table, node.item_id)
            _write_line(out, node.type, title)
            if slug:
                _write_line(out, "code", slug)
            out.write("\n")
            return

        self._warn_skip(node.type, node.item_id)

    def _emit_record(self, out: TextIO, node: TreeNode) -> None:
        rec = _fetch_record(self.conn, node.item_id)
        if rec is None:
            self._warn_skip("missing-record", node.item_id)
            return
        content_type, content = rec
        if content_type in SKIP_LEAF_TYPES:
            self._warn_skip(content_type, node.item_id)
            return
        tag = _record_bbt_tag(content_type, content)
        if tag is None:
            self._warn_skip(content_type, node.item_id)
            return
        _write_line(out, tag, content)
        out.write("\n")

    def _walk(self, out: TextIO, node: TreeNode) -> None:
        if node.type in SKIP_CONTAINER_TYPES:
            self._warn_skip(node.type, node.item_id)
            return

        if node.type == "record" or node.type in (
            "devanagari",
            "bengali",
            "wfw-pairs",
            "table",
        ):
            if node.type != "record":
                self._warn_skip(node.type, node.item_id)
                return
            self._emit_record(out, node)
        elif node.type == "image":
            self._warn_skip("image", node.item_id)
            return
        else:
            self._emit_container(out, node)

        for child in node.children:
            self._walk(out, child)

    def export(self, out: TextIO) -> None:
        root = self.nodes[self.root_id]
        if root.type in ("book", "canto"):
            self._export_root_is_book = True
            self.book_title = _fetch_book(self.conn, root.item_id)[0]
        self._walk(out, root)
        _write_line(out, "stop", "end")
        out.write("\n")


def export_to_bbt(
    vidya_path: Path,
    parent: str,
    output_path: Path,
) -> None:
    conn = connect_vidya(vidya_path)
    try:
        root_id = resolve_root_item_id(conn, parent)
        exporter = BbtExporter(conn, root_id)
        with output_path.open("w", encoding="utf-8", newline="\n") as f:
            exporter.export(f)
    finally:
        conn.close()

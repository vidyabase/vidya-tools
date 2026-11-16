"""Export .vidya subtrees to Vedabase backbone JSON format."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from vidya_lib.bbt_export import (
    CONTAINER_TABLE,
    SKIP_CONTAINER_TYPES,
    SKIP_LEAF_TYPES,
    TreeNode,
    _fetch_book,
    _fetch_chapter,
    _fetch_record,
    _fetch_simple_title,
    _load_subtree,
    _load_url_path_codes,
    resolve_root_item_id,
)
from vidya_lib.item_ids import connect_vidya

# content_tree.type values exported as leaf records (only "record" has content)
NON_RECORD_LEAF_TYPES: frozenset[str] = frozenset(
    {"devanagari", "bengali", "wfw-pairs", "table", "image"}
)


def _record_type(content_type: str) -> str:
    # Vidya stores e.g. verse_text; backbone JSON uses verse-text
    return content_type.replace("_", "-")


class VbJsonExporter:
    def __init__(self, conn: sqlite3.Connection, root_id: int) -> None:
        self.conn = conn
        self.nodes = _load_subtree(conn, root_id)
        if root_id not in self.nodes:
            raise ValueError(f"Root item_id {root_id} not in library")
        self.root_id = root_id
        self.items: list[dict[str, Any]] = []
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

    def _item_id_in_use(self, item_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM content_tree WHERE item_id = ?", (item_id,)
        ).fetchone()
        return row is not None

    def _add(
        self,
        backbone: int | None,
        parent: int | None,
        record_type: str,
        data: str,
        partial_code: str = "",
    ) -> None:
        item: dict[str, Any] = {}
        if backbone is not None:
            item["backbone"] = backbone
        item["data"] = data
        if parent is not None:
            item["parent_backbone"] = parent
        item["partial_code"] = partial_code
        item["record_type"] = record_type
        self.items.append(item)

    def _parent(self, node: TreeNode) -> int | None:
        return None if node.item_id == self.root_id else node.parent_id

    def _emit_container(self, node: TreeNode) -> None:
        code = self._url_codes.get(node.item_id, "")
        parent = self._parent(node)

        if node.type in ("book", "canto"):
            title, kind = _fetch_book(self.conn, node.item_id)
            record_type = "canto" if kind == "canto" else "book"
            self._add(node.item_id, parent, record_type, title, code)
            return

        if node.type == "chapter":
            title, chapter_title = _fetch_chapter(self.conn, node.item_id)
            self._add(node.item_id, parent, "chapter", title, code)
            if chapter_title:
                # Chapter title has no tree node; it takes the reserved id
                # right after the chapter's id.
                title_id: int | None = node.item_id + 1
                if self._item_id_in_use(title_id):
                    print(
                        f"Warning: chapter {node.item_id}: id {title_id} is "
                        f"taken; exporting chapter-title without backbone.",
                        file=sys.stderr,
                    )
                    title_id = None
                self._add(title_id, node.item_id, "chapter-title", chapter_title)
            return

        if node.type in CONTAINER_TABLE:
            table = CONTAINER_TABLE[node.type][0]
            title = _fetch_simple_title(self.conn, table, node.item_id)
            self._add(node.item_id, parent, node.type, title, code)
            return

        self._warn_skip(node.type, node.item_id)

    def _emit_record(self, node: TreeNode) -> None:
        rec = _fetch_record(self.conn, node.item_id)
        if rec is None:
            self._warn_skip("missing-record", node.item_id)
            return
        content_type, content = rec
        record_type = _record_type(content_type)
        if record_type in SKIP_LEAF_TYPES:
            self._warn_skip(record_type, node.item_id)
            return
        self._add(node.item_id, self._parent(node), record_type, content)

    def _walk(self, node: TreeNode) -> None:
        if node.type in SKIP_CONTAINER_TYPES or node.type in NON_RECORD_LEAF_TYPES:
            self._warn_skip(node.type, node.item_id)
            return

        if node.type == "record":
            self._emit_record(node)
        else:
            self._emit_container(node)

        for child in node.children:
            self._walk(child)

    def export(self) -> list[dict[str, Any]]:
        self._walk(self.nodes[self.root_id])
        return self.items


def export_to_vb_json(
    vidya_path: Path,
    parent: str,
    output_path: Path,
) -> None:
    conn = connect_vidya(vidya_path)
    try:
        root_id = resolve_root_item_id(conn, parent)
        items = VbJsonExporter(conn, root_id).export()
    finally:
        conn.close()
    with output_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(items, f, ensure_ascii=False, indent=4)
        f.write("\n")

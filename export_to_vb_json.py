#!/usr/bin/env python3
"""
Export an item and its descendants from a .vidya library to Vedabase backbone JSON format.

Each item has backbone (item id), parent_backbone (parent id), partial_code,
record_type and data; see example/en-bbd.json.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from vidya_lib.vb_json_export import export_to_vb_json


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a .vidya subtree to Vedabase backbone JSON format.",
    )
    parser.add_argument(
        "vidya_file",
        type=Path,
        help="Path to the .vidya library",
    )
    parser.add_argument(
        "parent",
        help="Root item_id or url_path (e.g. /nl/library/pra/ or 3136028)",
    )
    parser.add_argument(
        "output_file",
        type=Path,
        help="Output JSON file path",
    )
    args = parser.parse_args()

    if not args.vidya_file.is_file():
        print(f"Error: not a file: {args.vidya_file}", file=sys.stderr)
        sys.exit(2)

    try:
        export_to_vb_json(args.vidya_file, args.parent, args.output_file)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Wrote {args.output_file}")


if __name__ == "__main__":
    main()

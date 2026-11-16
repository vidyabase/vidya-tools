#!/usr/bin/env python3
"""
Export an item and its descendants from a .vidya library to BBT importer text format.

The output tag set follows vedabase_editor backbone/matcher.py (same tags the
importer accepts).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from vidya_lib.bbt_export import export_to_bbt


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a .vidya subtree to BBT importer text format.",
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
        help="Output text file path",
    )
    args = parser.parse_args()

    if not args.vidya_file.is_file():
        print(f"Error: not a file: {args.vidya_file}", file=sys.stderr)
        sys.exit(2)

    try:
        export_to_bbt(args.vidya_file, args.parent, args.output_file)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Wrote {args.output_file}")


if __name__ == "__main__":
    main()

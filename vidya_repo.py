#!/usr/bin/env python3
"""
Create and maintain a vidya repo: a folder of static files that holds every
version of ONE .vidya library, plus SQL diffs between consecutive versions,
so a reader app can update itself from `db_meta.update_url`.

    python3 vidya_repo.py init REPO library.vidya --slug vedabase-en --base-url https://example.org/vidya/vedabase-en/
    python3 vidya_repo.py add REPO library.vidya --label "November fixes"
    python3 vidya_repo.py log REPO
    python3 vidya_repo.py verify REPO --deep
    python3 vidya_repo.py prune REPO --full 2-5
    python3 vidya_repo.py update installed.vidya --reader-version 2.0.14     # reference reader client

Upload the contents of REPO/public/ to the web server (repo.json last).
Input files are never modified: the tool stores a stamped copy.
Format: VIDYA-REPO-FORMAT.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from vidya_lib import repo as vr  # noqa: E402


def _log(msg: str) -> None:
    print(msg, flush=True)


def _parse_versions(spec: str) -> set[int]:
    """'2-5,7' -> {2, 3, 4, 5, 7}"""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
                if a > b:
                    raise ValueError
                out.update(range(a, b + 1))
            else:
                out.add(int(part))
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid version list {spec!r} (use e.g. 2-5,7)") from None
    return out


def _add_version_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--label", help="short human name for this version (e.g. '2026-11 corrections')")
    p.add_argument("--notes", help="release notes for this version")
    rec = p.add_mutually_exclusive_group()
    rec.add_argument("--recommended-reader", metavar="VERSION",
                     help="set recommended_min_reader_version (e.g. 2.0.14)")
    rec.add_argument("--clear-recommended-reader", action="store_true",
                     help="remove recommended_min_reader_version")
    req = p.add_mutually_exclusive_group()
    req.add_argument("--required-reader", metavar="VERSION",
                     help="set required_min_reader_version (e.g. 2.1.0)")
    req.add_argument("--clear-required-reader", action="store_true",
                     help="remove required_min_reader_version")
    p.add_argument("--dry-run", action="store_true", help="show what would happen; write nothing")


def _opts(args: argparse.Namespace) -> vr.VersionOptions:
    return vr.VersionOptions(
        label=args.label,
        notes=args.notes,
        recommended=args.recommended_reader,
        required=args.required_reader,
        clear_recommended=args.clear_recommended_reader,
        clear_required=args.clear_required_reader,
        force=getattr(args, "force", False),
        verify=not getattr(args, "skip_verify", False),
        dry_run=args.dry_run,
    )


def cmd_init(args: argparse.Namespace) -> int:
    vr.init_repo(args.repo, args.file, slug=args.slug, base_url=args.base_url, title=args.title,
                 opts=_opts(args), log=_log)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    vr.add_version(args.repo, args.file, _opts(args), log=_log)
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    m = vr.load_manifest(args.repo)
    print(f"{m['title']}  (slug {m['slug']}, latest {m['latest']})")
    print(f"update_url: {m['update_url']}")
    reach = vr.versions_reaching_latest(m)
    print(f"{'ver':>5}  {'created':20}  {'full':>9}  {'diff':>9}  {'recommended':>11}  {'required':>9}  label")
    for e in reversed(m["versions"]):
        full = vr.human_size(e["full"]["size"]) if e.get("full") else "-"
        diff = vr.human_size(e["diff"]["size"]) if e.get("diff") else "-"
        mark = "" if e["version"] in reach else "  (no update path)"
        print(
            f"{e['version']:>5}  {e.get('created_at') or '':20}  {full:>9}  {diff:>9}"
            f"  {e.get(vr.KEY_RECOMMENDED) or '-':>11}  {e.get(vr.KEY_REQUIRED) or '-':>9}"
            f"  {e.get('label') or ''}{mark}"
        )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    res = vr.verify_repo(args.repo, deep=args.deep, log=_log if args.verbose else (lambda _m: None))
    for w in res.warnings:
        print(f"warning: {w}")
    for e in res.errors:
        print(f"error: {e}")
    print("ok" if res.ok else f"FAILED: {len(res.errors)} error(s)")
    return 0 if res.ok else 1


def cmd_prune(args: argparse.Namespace) -> int:
    if not args.full and not args.diff:
        print("error: give --full and/or --diff", file=sys.stderr)
        return 1
    vr.prune(args.repo, full=args.full, diffs=args.diff, dry_run=args.dry_run, log=_log)
    return 0


def cmd_rsync(args: argparse.Namespace) -> int:
    vr.rsync_upload(args.repo, args.dest, delete=args.delete, dry_run=args.dry_run,
                    verify=not args.skip_verify, rsync_args=tuple(args.rsync_arg), log=_log)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    plan = vr.client_update(args.file, repo_url=args.repo_url, reader_version=args.reader_version,
                            yes=args.yes, dry_run=args.dry_run, log=_log)
    if plan.status == "up-to-date" or (plan.steps and plan.status == "update"):
        return 0
    return 3  # the user has to act (upgrade the reader or download the full file)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create and maintain a vidya repo (versioned .vidya library + SQL diffs, served as static files)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a repo with FILE as version 1")
    p.add_argument("repo", type=Path, help="repo folder (created; REPO/public/ is what you upload)")
    p.add_argument("file", type=Path, help="the first .vidya file")
    p.add_argument("--slug", required=True, help="repo name used in file names, e.g. vedabase-en")
    p.add_argument("--base-url", required=True,
                   help="public URL of REPO/public/; update_url becomes BASE_URL/repo.json")
    p.add_argument("--title", help="repo title (default: library_title / db_name of FILE)")
    _add_version_args(p)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add", help="add FILE as the next version (with a diff from the previous one)")
    p.add_argument("repo", type=Path)
    p.add_argument("file", type=Path, help="the new .vidya file (not modified)")
    _add_version_args(p)
    p.add_argument("--force", action="store_true", help="add the version even if the content did not change")
    p.add_argument("--skip-verify", action="store_true", help="do not test-apply the diff (faster)")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("log", help="list versions")
    p.add_argument("repo", type=Path)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("verify", help="check files and checksums against repo.json")
    p.add_argument("repo", type=Path)
    p.add_argument("--deep", action="store_true",
                   help="also replay all diffs from the oldest usable full file and compare with the latest")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("prune", help="delete old published files")
    p.add_argument("repo", type=Path)
    p.add_argument("--full", type=_parse_versions, metavar="VERSIONS",
                   help="remove the full files of these versions, e.g. 2-5 (never the latest)")
    p.add_argument("--diff", type=_parse_versions, metavar="VERSIONS",
                   help="remove the diffs leading TO these versions, e.g. 2-3"
                        " (readers on older versions then need a full download)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("rsync", help="upload REPO/public/ to DEST with rsync (files first, repo.json last)")
    p.add_argument("repo", type=Path)
    p.add_argument("dest", help="rsync destination folder, e.g. server:/var/www/vidya/vedabase-en")
    p.add_argument("--delete", action="store_true",
                   help="after uploading repo.json, delete files on DEST that are not in REPO/public/ (after prune)")
    p.add_argument("--dry-run", action="store_true", help="pass --dry-run to rsync; upload nothing")
    p.add_argument("--skip-verify", action="store_true", help="do not run verify before uploading")
    p.add_argument("--rsync-arg", action="append", default=[], metavar="ARG",
                   help="extra rsync argument, repeatable (e.g. --rsync-arg=-e --rsync-arg='ssh -p 2222')")
    p.set_defaults(func=cmd_rsync)

    p = sub.add_parser("update", help="update a local .vidya file from its repo, as a reader app would")
    p.add_argument("file", type=Path, help="installed .vidya file (changed in place, one diff at a time)")
    p.add_argument("--repo-url", help="repo.json URL or local path (default: db_meta.update_url of FILE)")
    p.add_argument("--reader-version", help="pretend to be this reader version (checks the min reader versions)")
    p.add_argument("--yes", action="store_true", help="update even if a newer reader is recommended")
    p.add_argument("--dry-run", action="store_true", help="show the plan only")
    p.set_defaults(func=cmd_update)

    args = parser.parse_args()
    try:
        return args.func(args)
    except vr.RepoError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

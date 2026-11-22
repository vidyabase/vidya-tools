"""Vidya repository: a static, versioned home for one .vidya library.

The repo format is described in VIDYA-REPO-FORMAT.md. This module holds the logic
used by vidya_repo.py: creating a repo, adding versions (with SQL diffs made by
vidya_sql_diff.py), pruning, verifying, and a reference update client that does
what a reader app should do.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import vidya_sql_diff as vsd  # noqa: E402
from vidya_lib.item_ids import set_db_meta, table_exists, utc_now_rfc3339  # noqa: E402

REPO_FORMAT = 1
MANIFEST_NAME = "repo.json"
PUBLIC_DIR = "public"
WORK_DIR = ".work"
LOCK_NAME = ".lock"
VERSION_DIGITS = 6

KEY_REPO_VERSION = "repo_version"
KEY_UPDATE_URL = "update_url"
KEY_LEGACY_UPDATE_URL = "update-url"
KEY_RECOMMENDED = "recommended_min_reader_version"
KEY_REQUIRED = "required_min_reader_version"
KEY_MODIFIED_AT = "modified_at"
# First key found is recorded as the version's format_version in repo.json.
FORMAT_VERSION_KEYS = ("vidya_format_version", "format_version", "scheme_version", "db_version")
# db_meta keys the repo tool writes; ignored when deciding whether content changed.
MANAGED_KEYS = frozenset(
    {KEY_REPO_VERSION, KEY_UPDATE_URL, KEY_LEGACY_UPDATE_URL, KEY_RECOMMENDED, KEY_REQUIRED, KEY_MODIFIED_AT}
)

GUARD_TABLE = "_vidya_repo_guard"
PATCH_TAIL = b"COMMIT;\nPRAGMA foreign_keys = ON;\n"

FULL_ZIP_LEVEL = 9
DIFF_ZIP_LEVEL = 9
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
CHUNK = 1 << 20

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
READER_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}$")

Log = Callable[[str], None]


def _quiet(_msg: str) -> None:
    pass


class RepoError(Exception):
    """A problem the user has to fix; the message says what went wrong."""


# --------------------------------------------------------------------------- names and values


def full_file_name(slug: str, version: int) -> str:
    return f"{slug}-{version:0{VERSION_DIGITS}d}.vidya.zip"


def diff_file_name(slug: str, from_version: int, to_version: int) -> str:
    return f"{slug}-{from_version:0{VERSION_DIGITS}d}-{to_version:0{VERSION_DIGITS}d}.sql.zip"


def install_name(slug: str) -> str:
    """File name a reader installs the library under (also the entry name inside full zips)."""
    return f"{slug}.vidya"


def check_slug(slug: str) -> str:
    if not SLUG_RE.match(slug or ""):
        raise RepoError(f"invalid slug {slug!r}: use lowercase letters, digits and single hyphens (e.g. vedabase-en)")
    return slug


def check_base_url(base_url: str) -> str:
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in ("http", "https", "file") or (parts.scheme != "file" and not parts.netloc):
        raise RepoError(f"invalid base URL {base_url!r}: use e.g. https://example.org/vidya/vedabase-en/")
    return base_url


def update_url_for(base_url: str) -> str:
    return base_url.rstrip("/") + "/" + MANIFEST_NAME


def check_reader_version(value: str) -> str:
    v = value.strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    if not READER_VERSION_RE.match(v):
        raise RepoError(f"invalid reader version {value!r}: use numbers like 2.0.14")
    return v


def reader_version_key(value: str) -> tuple[int, ...]:
    parts = [int(p) for p in check_reader_version(value).split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def reader_older(reader: str, minimum: str) -> bool:
    """True if reader version `reader` is lower than `minimum`."""
    return reader_version_key(reader) < reader_version_key(minimum)


def meta_text(value: Optional[str]) -> Optional[str]:
    """A db_meta value as plain text. JSON strings and numbers ('"1.0"', '7') are decoded."""
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return str(value)
    if isinstance(decoded, str):
        return decoded
    if isinstance(decoded, (int, float)) and not isinstance(decoded, bool):
        return str(decoded)
    return str(value)


def parse_repo_version(value: Optional[str]) -> Optional[int]:
    text = meta_text(value)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def human_size(n: Optional[int]) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(n)


# --------------------------------------------------------------------------- files and hashes


def sha256_file(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
            n += len(chunk)
    return n, h.hexdigest()


def zip_file(src: Path, dst: Path, entry: str, level: int) -> dict:
    """Pack src into a zip at dst holding one entry named `entry`.

    Reproducible: fixed entry date and attributes, so the same input gives the same zip.
    Returns size/sha256 of the zip and of the unpacked content.
    """
    raw = hashlib.sha256()
    raw_n = 0
    info = zipfile.ZipInfo(entry, date_time=ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    for attr in ("compress_level", "_compresslevel"):  # public name since Python 3.13
        if hasattr(info, attr):
            setattr(info, attr, level)
    big = Path(src).stat().st_size >= zipfile.ZIP64_LIMIT
    with open(src, "rb") as fi, zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        with zf.open(info, "w", force_zip64=big) as w:
            for chunk in iter(lambda: fi.read(CHUNK), b""):
                raw.update(chunk)
                raw_n += len(chunk)
                w.write(chunk)
    size, sha = sha256_file(dst)
    return {"size": size, "sha256": sha, "raw_size": raw_n, "raw_sha256": raw.hexdigest()}


def _unzip_stream(source, name: str, out=None) -> tuple[int, str, str]:
    """Read the single entry of a zip (path or file object). Returns raw size, sha256, entry name."""
    h = hashlib.sha256()
    n = 0
    try:
        with zipfile.ZipFile(source) as zf:
            entries = [i for i in zf.infolist() if not i.is_dir()]
            if len(entries) != 1:
                raise RepoError(f"{name}: expected exactly one file inside the zip, found {len(entries)}")
            with zf.open(entries[0]) as r:
                for chunk in iter(lambda: r.read(CHUNK), b""):
                    h.update(chunk)
                    n += len(chunk)
                    if out is not None:
                        out.write(chunk)
            return n, h.hexdigest(), entries[0].filename
    except (zipfile.BadZipFile, zlib.error, OSError, EOFError) as e:
        raise RepoError(f"{name}: not a valid zip file ({e})") from e


def unzip_file(src: Path, dst: Optional[Path] = None) -> tuple[int, str]:
    """Unpack the single entry of zip src into dst (or only hash it when dst is None)."""
    src = Path(src)
    if dst is None:
        n, sha, _ = _unzip_stream(src, src.name)
        return n, sha
    with open(dst, "wb") as out:
        n, sha, _ = _unzip_stream(src, src.name, out)
    return n, sha


def _publish_zip(src: Path, dst: Path, entry: str, level: int) -> dict:
    tmp = dst.with_name(dst.name + ".tmp")
    info = zip_file(src, tmp, entry, level)
    os.replace(tmp, dst)
    return {"file": dst.name, "entry": entry, **info}


# --------------------------------------------------------------------------- db_meta


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def read_meta(conn: sqlite3.Connection) -> dict[str, Optional[str]]:
    if not table_exists(conn, "db_meta"):
        raise RepoError("not a .vidya library (missing db_meta table)")
    return {k: v for k, v in conn.execute("SELECT key, value FROM db_meta ORDER BY rowid")}


def read_meta_file(path: Path) -> dict[str, Optional[str]]:
    if not Path(path).is_file():
        raise RepoError(f"file not found: {path}")
    try:
        conn = _connect_ro(path)
        try:
            return read_meta(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise RepoError(f"{path}: not a readable .vidya (SQLite) file ({e})") from e


def format_version_of(meta: dict[str, Optional[str]]) -> Optional[str]:
    for key in FORMAT_VERSION_KEYS:
        text = meta_text(meta.get(key))
        if text:
            return text
    return None


def title_of(meta: dict[str, Optional[str]]) -> Optional[str]:
    for key in ("library_title", "db_name"):
        text = meta_text(meta.get(key))
        if text:
            return text
    return None


def stamp_copy(
    src: Path,
    dst: Path,
    *,
    version: int,
    update_url: str,
    recommended: Optional[str],
    required: Optional[str],
) -> None:
    """Copy src into dst (SQLite backup API) and write the repo keys into the copy's db_meta."""
    if dst.exists():
        dst.unlink()
    s = _connect_ro(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        s.close()
    try:
        d.execute("PRAGMA journal_mode = DELETE")
        with d:
            set_db_meta(d, KEY_REPO_VERSION, str(version))
            set_db_meta(d, KEY_UPDATE_URL, update_url)
            d.execute("DELETE FROM db_meta WHERE key = ?", (KEY_LEGACY_UPDATE_URL,))
            for key, value in ((KEY_RECOMMENDED, recommended), (KEY_REQUIRED, required)):
                if value is None:
                    d.execute("DELETE FROM db_meta WHERE key = ?", (key,))
                else:
                    set_db_meta(d, key, value)
            set_db_meta(d, KEY_MODIFIED_AT, utc_now_rfc3339())
    finally:
        d.close()


# --------------------------------------------------------------------------- manifest


def public_dir(repo: Path) -> Path:
    return Path(repo) / PUBLIC_DIR


def manifest_path(repo: Path) -> Path:
    return public_dir(repo) / MANIFEST_NAME


def load_manifest(repo: Path) -> dict:
    p = manifest_path(repo)
    if not p.is_file():
        raise RepoError(f"not a vidya repo: {p} not found")
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise RepoError(f"{p}: invalid JSON ({e})") from e
    if not isinstance(m, dict) or m.get("format") != REPO_FORMAT:
        raise RepoError(f"{p}: unsupported repo format {m.get('format') if isinstance(m, dict) else '?'!r}")
    return m


def save_manifest(repo: Path, manifest: dict) -> None:
    p = manifest_path(repo)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def version_entry(manifest: dict, version: int) -> Optional[dict]:
    for e in manifest.get("versions", []):
        if e.get("version") == version:
            return e
    return None


def versions_reaching_latest(manifest: dict) -> set[int]:
    """Versions from which the latest version can be reached through published diffs."""
    latest = manifest["latest"]
    ok = {latest}
    v = latest
    while v > 1:
        e = version_entry(manifest, v)
        d = e.get("diff") if e else None
        if not d or d.get("from") != v - 1:
            break
        v -= 1
        ok.add(v)
    return ok


class RepoLock:
    """Exclusive lock on a repo (a file created with O_EXCL)."""

    def __init__(self, repo: Path):
        self.path = Path(repo) / LOCK_NAME

    def __enter__(self) -> "RepoLock":
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RepoError(
                f"repo is locked by another run ({self.path}); if no other run is active, delete that file"
            ) from None
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class _Workspace:
    """Scratch folder inside the repo (outside public/), removed afterwards."""

    def __init__(self, repo: Path):
        self.path = Path(repo) / WORK_DIR

    def __enter__(self) -> Path:
        if self.path.exists():
            shutil.rmtree(self.path)
        self.path.mkdir(parents=True)
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _clean_tmp_files(public: Path) -> None:
    for p in public.glob("*.tmp"):
        p.unlink()


# --------------------------------------------------------------------------- patches


def _guard_sql(stage: str, version: int) -> str:
    return (
        f"-- vidya repo guard: repo_version must be {version} at the {stage}\n"
        f"INSERT OR ROLLBACK INTO temp.{GUARD_TABLE} (stage, ok) SELECT '{stage}', COUNT(*) FROM db_meta"
        f" WHERE key = '{KEY_REPO_VERSION}' AND trim(value, '\"') = '{version}';\n"
    )


def guard_prologue(from_version: int) -> str:
    return (
        f"\nDROP TABLE IF EXISTS temp.{GUARD_TABLE};\n"
        f"CREATE TEMP TABLE {GUARD_TABLE} (\n"
        "  stage TEXT NOT NULL,\n  ok INTEGER NOT NULL,\n"
        "  CONSTRAINT start_version_mismatch CHECK (stage <> 'start' OR ok = 1),\n"
        "  CONSTRAINT end_version_mismatch CHECK (stage <> 'end' OR ok = 1)\n);\n"
        + _guard_sql("start", from_version)
    )


def guard_epilogue(to_version: int) -> str:
    return "\n" + _guard_sql("end", to_version) + f"DROP TABLE temp.{GUARD_TABLE};\n\n"


def write_guarded_patch(src: Path, dst: Path, from_version: int, to_version: int) -> None:
    """Copy a vidya_sql_diff patch, adding version guards after BEGIN and before COMMIT."""
    size = src.stat().st_size
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        pos = 0
        while True:
            line = fi.readline()
            if not line:
                raise RepoError("unexpected patch layout (no BEGIN line)")
            fo.write(line)
            pos += len(line)
            if line == b"BEGIN;\n":
                break
        fo.write(guard_prologue(from_version).encode("utf-8"))
        fi.seek(size - len(PATCH_TAIL))
        if fi.read() != PATCH_TAIL:
            raise RepoError("unexpected patch layout (does not end with COMMIT)")
        fi.seek(pos)
        remaining = size - len(PATCH_TAIL) - pos
        while remaining > 0:
            chunk = fi.read(min(CHUNK, remaining))
            if not chunk:
                break
            fo.write(chunk)
            remaining -= len(chunk)
        fo.write(guard_epilogue(to_version).encode("utf-8"))
        fo.write(PATCH_TAIL)


def apply_patch(db_path: Path, sql: str, expected_version: int) -> None:
    """Apply a repo diff to db_path. It applies fully or not at all."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        try:
            conn.executescript(sql)
        except sqlite3.DatabaseError as e:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            msg = str(e)
            if "start_version_mismatch" in msg:
                raise RepoError(
                    f"the file is not at the version this diff starts from (diff leads to {expected_version})"
                ) from e
            if "end_version_mismatch" in msg:
                raise RepoError(f"after the diff the file is not at version {expected_version}") from e
            raise RepoError(f"diff failed to apply: {e}") from e
        conn.execute("PRAGMA foreign_keys = ON")
        got = parse_repo_version(read_meta(conn).get(KEY_REPO_VERSION))
        if got != expected_version:
            raise RepoError(f"after the diff repo_version is {got}, expected {expected_version}")
    finally:
        conn.close()


def same_content(a: Path, b: Path) -> tuple[bool, list[str]]:
    """Whether two .vidya files hold the same schema and data (any byte layout)."""
    with tempfile.TemporaryFile("w+", encoding="utf-8", newline="") as out:
        vsd.generate_patch(Path(a), Path(b), out, set())
        out.seek(0)
        head = [out.readline() for _ in range(8)]
        if any("nothing to do" in line for line in head):
            return True, []
        out.seek(0)
        rest: list[str] = []
        for line in out:
            if line.strip() and not line.startswith("--"):
                rest.append(line.rstrip()[:200])
                if len(rest) >= 10:
                    break
        return False, rest


def _content_unchanged(prev_db: Path, new_db: Path, stats: "vsd.Stats") -> bool:
    """True if only the repo-managed db_meta keys differ between the two files."""
    if set(stats.tables_changed) - {"db_meta"}:
        return False
    conns = [_connect_ro(prev_db), _connect_ro(new_db)]
    try:
        views = []
        for c in conns:
            schema = c.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
            uv = c.execute("PRAGMA user_version").fetchone()[0]
            meta = {k: v for k, v in read_meta(c).items() if k not in MANAGED_KEYS}
            views.append((schema, uv, meta))
        return views[0] == views[1]
    finally:
        for c in conns:
            c.close()


# --------------------------------------------------------------------------- creating and adding


@dataclass
class VersionOptions:
    label: Optional[str] = None
    notes: Optional[str] = None
    recommended: Optional[str] = None
    required: Optional[str] = None
    clear_recommended: bool = False
    clear_required: bool = False
    force: bool = False
    verify: bool = True
    dry_run: bool = False


def _resolve_reader_version(
    key: str, flag: Optional[str], clear: bool, input_meta: dict, prev: Optional[dict]
) -> tuple[Optional[str], str]:
    """Value for a reader-version key, and where it came from."""
    if clear:
        return None, "cleared"
    if flag:
        return check_reader_version(flag), "option"
    from_file = meta_text(input_meta.get(key))
    if from_file:
        return check_reader_version(from_file), "input file"
    if prev and prev.get(key):
        return prev[key], f"version {prev['version']}"
    return None, "not set"


def _reader_versions(opts: VersionOptions, input_meta: dict, prev: Optional[dict], log: Log) -> tuple:
    rec, rec_src = _resolve_reader_version(KEY_RECOMMENDED, opts.recommended, opts.clear_recommended, input_meta, prev)
    req, req_src = _resolve_reader_version(KEY_REQUIRED, opts.required, opts.clear_required, input_meta, prev)
    log(f"recommended min reader: {rec or '-'} ({rec_src})")
    log(f"required min reader:    {req or '-'} ({req_src})")
    if rec and req and reader_older(rec, req):
        log(f"warning: recommended reader {rec} is lower than required reader {req}")
    old_req = prev.get(KEY_REQUIRED) if prev else None
    if old_req and (req is None or reader_older(req, old_req)):
        log(f"warning: required min reader goes down from {old_req} to {req or '-'}")
    return rec, req


def _version_record(version: int, opts: VersionOptions, meta: dict, rec, req, full: dict, diff: Optional[dict]) -> dict:
    return {
        "version": version,
        "created_at": utc_now_rfc3339(),
        "label": opts.label,
        "notes": opts.notes,
        "format_version": format_version_of(meta),
        KEY_RECOMMENDED: rec,
        KEY_REQUIRED: req,
        "full": full,
        "diff": diff,
    }


def init_repo(
    repo: Path,
    input_file: Path,
    *,
    slug: str,
    base_url: str,
    title: Optional[str] = None,
    opts: Optional[VersionOptions] = None,
    log: Log = _quiet,
) -> Optional[dict]:
    """Create a repo with input_file as version 1. Returns the manifest (None on a dry run)."""
    opts = opts or VersionOptions()
    repo = Path(repo)
    check_slug(slug)
    update_url = update_url_for(check_base_url(base_url))
    if manifest_path(repo).exists():
        raise RepoError(f"{repo} is already a vidya repo")
    meta = read_meta_file(input_file)
    title = title or title_of(meta) or slug
    log(f"repo:       {repo}  (slug {slug}, title {title!r})")
    log(f"update_url: {update_url}")
    log("version:    1")
    rec, req = _reader_versions(opts, meta, None, log)
    if opts.dry_run:
        log("dry run: nothing written")
        return None

    public = public_dir(repo)
    public.mkdir(parents=True, exist_ok=True)
    with RepoLock(repo), _Workspace(repo) as work:
        _clean_tmp_files(public)
        db = work / f"{slug}-{1:0{VERSION_DIGITS}d}.vidya"
        log("copying and stamping the file")
        stamp_copy(input_file, db, version=1, update_url=update_url, recommended=rec, required=req)
        log("packing full file")
        full = _publish_zip(db, public / full_file_name(slug, 1), install_name(slug), FULL_ZIP_LEVEL)
        manifest = {
            "format": REPO_FORMAT,
            "slug": slug,
            "title": title,
            "update_url": update_url,
            "install_file_name": install_name(slug),
            "latest": 1,
            "updated_at": utc_now_rfc3339(),
            "versions": [_version_record(1, opts, read_meta_file(db), rec, req, full, None)],
        }
        save_manifest(repo, manifest)
    log(f"created version 1: {full['file']} ({human_size(full['size'])})")
    return manifest


def add_version(repo: Path, input_file: Path, opts: Optional[VersionOptions] = None, log: Log = _quiet) -> Optional[dict]:
    """Add input_file as the next version. Returns the new version entry (None on a dry run)."""
    opts = opts or VersionOptions()
    repo = Path(repo)
    manifest = load_manifest(repo)
    slug = manifest["slug"]
    update_url = manifest["update_url"]
    latest = manifest["latest"]
    prev = version_entry(manifest, latest)
    if not prev or not prev.get("full"):
        raise RepoError(f"latest version {latest} has no full file in repo.json; the repo is damaged")
    meta = read_meta_file(input_file)
    version = latest + 1
    log(f"repo:    {repo}  (slug {slug})")
    log(f"version: {version}  (diff from {latest})")
    old_url = meta_text(meta.get(KEY_UPDATE_URL)) or meta_text(meta.get(KEY_LEGACY_UPDATE_URL))
    if old_url != update_url:
        log(f"update_url: {old_url or '-'} -> {update_url}")
    rec, req = _reader_versions(opts, meta, prev, log)
    if opts.dry_run:
        log("dry run: nothing written")
        return None

    public = public_dir(repo)
    with RepoLock(repo), _Workspace(repo) as work:
        _clean_tmp_files(public)
        prev_db = work / f"{slug}-{latest:0{VERSION_DIGITS}d}.vidya"
        log(f"unpacking version {latest}")
        _, raw_sha = unzip_file(public / prev["full"]["file"], prev_db)
        if raw_sha != prev["full"]["raw_sha256"]:
            raise RepoError(f"stored file of version {latest} does not match its checksum; run verify")

        new_db = work / f"{slug}-{version:0{VERSION_DIGITS}d}.vidya"
        log("copying and stamping the new file")
        stamp_copy(input_file, new_db, version=version, update_url=update_url, recommended=rec, required=req)

        log("generating diff")
        raw_patch = work / "patch.sql"
        with open(raw_patch, "w", encoding="utf-8", newline="") as fh:
            stats = vsd.generate_patch(prev_db, new_db, fh, set())
        if _content_unchanged(prev_db, new_db, stats):
            if not opts.force:
                raise RepoError(f"no content changes since version {latest} (use --force to add it anyway)")
            log("note: no content changes (added because of --force)")
        log(
            f"  rows: {stats.inserted} insert, {stats.updated} update, {stats.deleted} delete"
            f" in {len(stats.tables_changed)} table(s)"
        )
        patch = work / f"{slug}-{latest:0{VERSION_DIGITS}d}-{version:0{VERSION_DIGITS}d}.sql"
        write_guarded_patch(raw_patch, patch, latest, version)
        raw_patch.unlink()

        if opts.verify:
            log("verifying diff")
            apply_patch(prev_db, patch.read_text(encoding="utf-8"), version)
            ok, rest = same_content(prev_db, new_db)
            if not ok:
                raise RepoError("verification failed, differences remain:\n  " + "\n  ".join(rest))

        log("packing full file and diff")
        full = _publish_zip(new_db, public / full_file_name(slug, version), install_name(slug), FULL_ZIP_LEVEL)
        diff = {
            "from": latest,
            **_publish_zip(patch, public / diff_file_name(slug, latest, version), patch.name, DIFF_ZIP_LEVEL),
            "stats": {
                "insert": stats.inserted,
                "update": stats.updated,
                "delete": stats.deleted,
                "tables": sorted(stats.tables_changed),
            },
        }
        entry = _version_record(version, opts, read_meta_file(new_db), rec, req, full, diff)
        manifest["versions"].append(entry)
        manifest["latest"] = version
        manifest["updated_at"] = utc_now_rfc3339()
        save_manifest(repo, manifest)
    log(
        f"added version {version}: {full['file']} ({human_size(full['size'])}),"
        f" {diff['file']} ({human_size(diff['size'])})"
    )
    if full["size"] and diff["size"] > full["size"] // 2:
        log("warning: the diff is more than half the size of the full file")
    return entry


# --------------------------------------------------------------------------- prune


def prune(
    repo: Path,
    *,
    full: Optional[set[int]] = None,
    diffs: Optional[set[int]] = None,
    dry_run: bool = False,
    log: Log = _quiet,
) -> list[int]:
    """Delete published full files and/or diffs (diffs named by the version they lead to).

    Returns the versions whose readers can no longer update automatically.
    """
    full, diffs = set(full or ()), set(diffs or ())
    repo = Path(repo)
    manifest = load_manifest(repo)
    latest = manifest["latest"]
    if latest in full:
        raise RepoError(f"the full file of the latest version ({latest}) cannot be removed")
    new = copy.deepcopy(manifest)
    to_delete: list[str] = []
    for kind, versions in (("full", full), ("diff", diffs)):
        for v in sorted(versions):
            e = version_entry(new, v)
            if e is None:
                raise RepoError(f"version {v} does not exist")
            if not e.get(kind):
                log(f"version {v}: no {kind} file (already removed or never had one)")
                continue
            to_delete.append(e[kind]["file"])
            log(f"version {v}: remove {kind} {e[kind]['file']} ({human_size(e[kind]['size'])})")
            e[kind] = None
    lost = sorted(versions_reaching_latest(manifest) - versions_reaching_latest(new))
    if lost:
        log(
            "warning: readers at version(s) " + ", ".join(map(str, lost))
            + " will no longer update automatically (they must download the latest full file)"
        )
    if not to_delete:
        log("nothing to remove")
        return lost
    if dry_run:
        log("dry run: nothing changed")
        return lost
    with RepoLock(repo):
        new["updated_at"] = utc_now_rfc3339()
        save_manifest(repo, new)  # repo.json first: it never points to a missing file
        for name in to_delete:
            try:
                (public_dir(repo) / name).unlink()
            except FileNotFoundError:
                pass
    log(f"removed {len(to_delete)} file(s)")
    return lost


# --------------------------------------------------------------------------- verify


@dataclass
class VerifyResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _check_file(public: Path, info: dict, what: str, res: VerifyResult) -> None:
    p = public / info.get("file", "")
    if not info.get("file") or not p.is_file():
        res.errors.append(f"{what}: file missing: {info.get('file')}")
        return
    size, sha = sha256_file(p)
    if size != info.get("size") or sha != info.get("sha256"):
        res.errors.append(f"{what}: {p.name} does not match size/sha256 in repo.json")
        return
    try:
        raw_size, raw_sha, entry = _unzip_stream(p, p.name)
    except RepoError as e:
        res.errors.append(f"{what}: {e}")
        return
    if raw_size != info.get("raw_size") or raw_sha != info.get("raw_sha256"):
        res.errors.append(f"{what}: unpacked {p.name} does not match raw_size/raw_sha256 in repo.json")
    if info.get("entry") and entry != info["entry"]:
        res.errors.append(f"{what}: {p.name} contains {entry!r}, repo.json says {info['entry']!r}")


def verify_repo(repo: Path, *, deep: bool = False, log: Log = _quiet) -> VerifyResult:
    repo = Path(repo)
    res = VerifyResult()
    manifest = load_manifest(repo)
    public = public_dir(repo)
    slug = manifest.get("slug", "")
    try:
        check_slug(slug)
    except RepoError as e:
        res.errors.append(str(e))
    latest = manifest.get("latest")
    versions = [e.get("version") for e in manifest.get("versions", [])]
    if not isinstance(latest, int) or versions != list(range(1, (latest or 0) + 1)):
        res.errors.append(f"versions in repo.json must be 1..latest in order (latest={latest!r}, found {versions})")
        return res

    referenced = {MANIFEST_NAME}
    for e in manifest["versions"]:
        v = e["version"]
        if e.get("full"):
            referenced.add(e["full"].get("file"))
            if e["full"].get("file") != full_file_name(slug, v):
                res.warnings.append(f"version {v}: unexpected full file name {e['full'].get('file')}")
            _check_file(public, e["full"], f"version {v} full", res)
        elif v == latest:
            res.errors.append(f"latest version {v} has no full file")
        if e.get("diff"):
            d = e["diff"]
            referenced.add(d.get("file"))
            if d.get("from") != v - 1:
                res.errors.append(f"version {v}: diff must start from {v - 1}, not {d.get('from')}")
            _check_file(public, d, f"version {v} diff", res)
        for key in (KEY_RECOMMENDED, KEY_REQUIRED):
            if e.get(key):
                try:
                    check_reader_version(e[key])
                except RepoError as err:
                    res.errors.append(f"version {v}: {key}: {err}")
        log(f"checked version {v}")
    for p in sorted(public.iterdir()):
        if p.name not in referenced:
            res.warnings.append(f"file not listed in repo.json: {p.name}")
    reach = versions_reaching_latest(manifest)
    stuck = [v for v in versions if v not in reach]
    if stuck:
        res.warnings.append(
            "readers at version(s) " + ", ".join(map(str, stuck)) + " cannot update automatically (diffs removed)"
        )
    if deep and res.ok:
        _deep_verify(repo, manifest, reach, res, log)
    return res


def _deep_verify(repo: Path, manifest: dict, reach: set[int], res: VerifyResult, log: Log) -> None:
    public = public_dir(repo)
    slug, latest = manifest["slug"], manifest["latest"]
    start = min(v for v in reach if version_entry(manifest, v).get("full"))
    with RepoLock(repo), _Workspace(repo) as work:
        db = work / "replay.vidya"
        unzip_file(public / version_entry(manifest, start)["full"]["file"], db)
        got = parse_repo_version(read_meta_file(db).get(KEY_REPO_VERSION))
        if got != start:
            res.errors.append(f"full file of version {start} has repo_version {got}")
            return
        for v in range(start + 1, latest + 1):
            d = version_entry(manifest, v)["diff"]
            sql = work / "step.sql"
            unzip_file(public / d["file"], sql)
            try:
                apply_patch(db, sql.read_text(encoding="utf-8"), v)
            except RepoError as e:
                res.errors.append(f"replaying diff to version {v}: {e}")
                return
            log(f"replayed diff {v - 1} -> {v}")
        latest_db = work / "latest.vidya"
        unzip_file(public / version_entry(manifest, latest)["full"]["file"], latest_db)
        ok, rest = same_content(db, latest_db)
        if not ok:
            res.errors.append(
                f"replaying diffs from version {start} does not give version {latest}:\n  " + "\n  ".join(rest)
            )
            return
        meta = read_meta_file(latest_db)
        if meta_text(meta.get(KEY_UPDATE_URL)) != manifest["update_url"]:
            res.warnings.append("update_url in the latest file differs from repo.json")
        log(f"replay from version {start} to {latest} matches the latest full file")


# --------------------------------------------------------------------------- reference update client


@dataclass
class UpdatePlan:
    """What a reader should do. status: up-to-date | update | partial | blocked | manual."""

    status: str
    local_version: Optional[int]
    latest: int
    steps: list[dict] = field(default_factory=list)  # version entries to apply, in order
    stop_reason: Optional[str] = None  # no-repo-version | missing-diff | required-reader
    stop_version: Optional[int] = None  # first version that cannot be applied
    required_reader: Optional[str] = None  # reader version needed for stop_version
    recommended_reader: Optional[str] = None  # highest recommended version (among steps) the reader is below

    @property
    def target(self) -> Optional[int]:
        return self.steps[-1]["version"] if self.steps else self.local_version


def plan_update(manifest: dict, local_version: Optional[int], reader_version: Optional[str] = None) -> UpdatePlan:
    latest = manifest["latest"]
    plan = UpdatePlan(status="update", local_version=local_version, latest=latest)
    if local_version is None:
        plan.status, plan.stop_reason = "manual", "no-repo-version"
        return plan
    if local_version >= latest:
        plan.status = "up-to-date"
        return plan
    for v in range(local_version + 1, latest + 1):
        e = version_entry(manifest, v)
        d = e.get("diff") if e else None
        if not d or d.get("from") != v - 1:
            plan.stop_reason, plan.stop_version = "missing-diff", v
            break
        req = e.get(KEY_REQUIRED)
        if reader_version and req and reader_older(reader_version, req):
            plan.stop_reason, plan.stop_version, plan.required_reader = "required-reader", v, req
            break
        plan.steps.append(e)
    if reader_version:
        recs = [e[KEY_RECOMMENDED] for e in plan.steps if e.get(KEY_RECOMMENDED)]
        recs = [r for r in recs if reader_older(reader_version, r)]
        if recs:
            plan.recommended_reader = max(recs, key=reader_version_key)
    if plan.stop_reason:
        if plan.steps:
            plan.status = "partial"
        else:
            plan.status = "blocked" if plan.stop_reason == "required-reader" else "manual"
    return plan


def _as_url(location: str) -> str:
    if urllib.parse.urlsplit(location).scheme in ("http", "https", "file"):
        return location
    return Path(location).resolve().as_uri()


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "vidya-repo/1"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except OSError as e:
        raise RepoError(f"download failed: {url} ({e})") from e


def decode_download(data: bytes, info: dict) -> bytes:
    """Check a downloaded zip against repo.json and return the unpacked content of its one entry."""
    if hashlib.sha256(data).hexdigest() != info["sha256"]:
        raise RepoError(f"{info['file']}: checksum mismatch")
    out = io.BytesIO()
    _, raw_sha, _ = _unzip_stream(io.BytesIO(data), info["file"], out)
    if raw_sha != info["raw_sha256"]:
        raise RepoError(f"{info['file']}: unpacked checksum mismatch")
    return out.getvalue()


def describe_plan(plan: UpdatePlan, manifest: dict, manifest_url: str, reader_version: Optional[str]) -> list[str]:
    lines = [f"local version: {plan.local_version if plan.local_version is not None else '-'}, latest: {plan.latest}"]
    if plan.status == "up-to-date":
        lines.append("up to date")
        return lines
    if plan.steps:
        vs = [str(e["version"]) for e in plan.steps]
        size = sum(e["diff"]["size"] for e in plan.steps)
        lines.append(f"can update to version {plan.target}: {len(vs)} diff(s), {human_size(size)} ({', '.join(vs)})")
    if plan.recommended_reader:
        lines.append(
            f"reader {plan.recommended_reader} or newer is recommended for this update (this reader: {reader_version});"
            " upgrade the reader first, or update anyway"
        )
    if plan.stop_reason == "required-reader":
        lines.append(
            f"version {plan.stop_version} requires reader {plan.required_reader} or newer"
            f" (this reader: {reader_version}): upgrade the reader to update further"
        )
    elif plan.stop_reason in ("missing-diff", "no-repo-version"):
        why = "the file has no repo_version" if plan.stop_reason == "no-repo-version" else (
            f"no diff is published for version {plan.stop_version}"
        )
        full = version_entry(manifest, plan.latest)["full"]
        lines.append(
            f"{why}: download the latest full file and add it to the reader:\n"
            f"  {urllib.parse.urljoin(manifest_url, full['file'])}  (install as {manifest['install_file_name']})"
        )
    return lines


def client_update(
    db_path: Path,
    *,
    repo_url: Optional[str] = None,
    reader_version: Optional[str] = None,
    yes: bool = False,
    dry_run: bool = False,
    log: Log = _quiet,
) -> UpdatePlan:
    """Update a local .vidya file from its repo, as a reader app would."""
    db_path = Path(db_path)
    meta = read_meta_file(db_path)
    local = parse_repo_version(meta.get(KEY_REPO_VERSION))
    url = _as_url(repo_url) if repo_url else meta_text(meta.get(KEY_UPDATE_URL))
    if not url:
        raise RepoError("the file has no update_url (pass --repo-url)")
    if reader_version:
        check_reader_version(reader_version)
    try:
        manifest = json.loads(fetch_bytes(url).decode("utf-8"))
    except ValueError as e:
        raise RepoError(f"{url}: invalid repo.json ({e})") from e
    if manifest.get("format") != REPO_FORMAT:
        raise RepoError(f"repo format {manifest.get('format')!r} is not supported: upgrade the reader")
    plan = plan_update(manifest, local, reader_version)
    for line in describe_plan(plan, manifest, url, reader_version):
        log(line)
    if not plan.steps:
        return plan
    if plan.recommended_reader and not yes:
        log("not updated: rerun with --yes to update anyway")
        plan.steps = []
        return plan
    if dry_run:
        log("dry run: nothing changed")
        return plan
    for e in plan.steps:
        d = e["diff"]
        raw = decode_download(fetch_bytes(urllib.parse.urljoin(url, d["file"])), d)
        apply_patch(db_path, raw.decode("utf-8"), e["version"])
        log(f"applied {d['file']} -> version {e['version']}")
    return plan

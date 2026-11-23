# Vidya repo format

**Repo format version:** `1` (`repo.json` → `"format": 1`)

A **vidya repo** is a folder of static files that holds every version of **one** `.vidya` library, plus an SQL diff between each pair of consecutive versions. It is published on any web server (no server code). A reader app updates an installed library by downloading and applying the diffs, one after another, until it reaches the latest version.

The repo is created and maintained with [`vidya_repo.py`](README.md#vidya_repopy). This document is the contract between that tool and reader apps (Vidyabase).

---

## 1. Concepts

| Term | Meaning |
|------|---------|
| **library** | One `.vidya` file (SQLite). One repo = one library. Three libraries = three repos. |
| **version** | An integer `1, 2, 3, …`, increasing by one with each release. Stored as `db_meta.repo_version` inside the file. |
| **full file** | The complete `.vidya` file of a version, compressed with zstd. |
| **diff** | An SQL script that turns version `N-1` into version `N`. Made by `vidya_sql_diff.py`, with version guards added (§6). |
| **slug** | The repo name used in file names, e.g. `vedabase-en`: lowercase letters, digits, single hyphens. |

Versions are never rewritten. To undo a release, publish a new version with the old content. Its diff is computed like any other.

---

## 2. Folder layout

```
REPO/                                  working folder (on the maintainer's machine)
├── public/                            ← upload this folder; update_url points into it
│   ├── repo.json                      manifest (the only file that changes)
│   ├── vedabase-en-000001.vidyapack    full file of version 1
│   ├── vedabase-en-000002.vidyapack    full file of version 2
│   ├── vedabase-en-000001-000002.sql.zst   diff 1 → 2
│   └── …
├── .lock                              exists only while the tool runs
└── .work/                             scratch folder, exists only while the tool runs
```

### File names

| File | Pattern | Example |
|------|---------|---------|
| Full file | `{slug}-{version:06d}.vidyapack` | `vedabase-en-000007.vidyapack` |
| Diff | `{slug}-{from:06d}-{to:06d}.sql.zst` | `vedabase-en-000006-000007.sql.zst` |
| Manifest | `repo.json` | |

Files other than `repo.json` are **immutable**: once published, a file with a given name never changes. Readers find files only through `repo.json`, never by building names themselves.

A full file is published under a versioned name, but it is **installed** under `install_file_name` from `repo.json` (e.g. `vedabase-en.vidya`). The reader keeps user data (bookmarks, notes, highlights) per file name, so the installed name must stay the same across versions.

### Zstandard files

Every published file except `repo.json` is a single [Zstandard](https://facebook.github.io/zstd/) frame (RFC 8878) holding **exactly one** file, compressed at level 19. Unpacked, a full file is the library under the name `install_file_name`, and a diff is the `.sql` text.

A user who downloads a full file by hand can unpack it with `zstd -d vedabase-en-000007.vidyapack -o vedabase-en.vidya` (or any tool that reads zstd) and import the result into the reader. Output is reproducible: the same content gives the same bytes.

---

## 3. `repo.json`

UTF-8 JSON. Readers must ignore unknown keys.

```json
{
  "format": 1,
  "slug": "vedabase-en",
  "title": "Vedabase English",
  "update_url": "https://example.org/vidya/vedabase-en/repo.json",
  "install_file_name": "vedabase-en.vidya",
  "latest": 2,
  "updated_at": "2026-11-22T18:37:53Z",
  "versions": [
    {
      "version": 1,
      "created_at": "2026-11-22T18:37:30Z",
      "label": null,
      "notes": null,
      "format_version": "1.0",
      "recommended_min_reader_version": null,
      "required_min_reader_version": null,
      "full": {
        "file": "vedabase-en-000001.vidyapack",
        "size": 84011434,
        "sha256": "…",
        "raw_size": 230572032,
        "raw_sha256": "…"
      },
      "diff": null
    },
    {
      "version": 2,
      "created_at": "2026-11-22T18:37:53Z",
      "label": "November corrections",
      "notes": null,
      "format_version": "1.0",
      "recommended_min_reader_version": "2.0.14",
      "required_min_reader_version": null,
      "full": { "file": "vedabase-en-000002.vidyapack", "size": 86272817, "sha256": "…", "raw_size": 232300544, "raw_sha256": "…" },
      "diff": {
        "from": 1,
        "file": "vedabase-en-000001-000002.sql.zst",
        "size": 2442545,
        "sha256": "…",
        "raw_size": 8754648,
        "raw_sha256": "…",
        "stats": { "insert": 4892, "update": 4917, "delete": 4891, "tables": ["content_fts", "db_meta", "records", "url_paths"] }
      }
    }
  ]
}
```

### Top level

| Key | Type | Meaning |
|-----|------|---------|
| `format` | int | Repo format version, `1`. A reader that does not know the value must not update and should tell the user to upgrade the reader. |
| `slug` | string | Repo name, used in file names. |
| `title` | string | Human title (from `library_title` / `db_name` of the first file, unless set with `--title`). |
| `update_url` | string | Public URL of this `repo.json`. Every version's file carries the same value in `db_meta.update_url`. |
| `install_file_name` | string | File name to install the library under, `{slug}.vidya`. |
| `latest` | int | Highest version. Its `full` file is always present. |
| `updated_at` | string | RFC 3339 UTC time of the last change to the repo. |
| `versions` | array | One entry per version, `1 … latest`, ascending, none missing. |

### Version entry

| Key | Type | Meaning |
|-----|------|---------|
| `version` | int | The version number. |
| `created_at` | string | When it was added (RFC 3339 UTC). |
| `label`, `notes` | string \| null | Optional human name and release notes (can be shown in the update dialog). |
| `format_version` | string \| null | The `.vidya` format version of this file: the first of `vidya_format_version`, `format_version`, `scheme_version`, `db_version` found in its `db_meta`. |
| `recommended_min_reader_version` | string \| null | See §5. |
| `required_min_reader_version` | string \| null | See §5. |
| `full` | file \| null | The full file. `null` if it was removed with `prune` (never for `latest`). |
| `diff` | file \| null | The diff from `version - 1`. `null` for version 1 and for removed diffs. |

### File object

| Key | Meaning |
|-----|---------|
| `file` | File name, relative to `repo.json` (resolve it against `update_url`). |
| `size`, `sha256` | Size and SHA-256 (lowercase hex) of the `.zst` file **as published**. |
| `raw_size`, `raw_sha256` | Size and SHA-256 of the **unpacked** content. |
| `from` | (diffs only) The version the diff starts from, always `version - 1`. |
| `stats` | (diffs only, informative) Row counts and changed tables. |

---

## 4. `db_meta` keys set by the repo

When a file is added, the tool copies it into the repo and writes these keys into the **copy's** `db_meta`. The input file is never changed. Values are plain text (not JSON-quoted).

| Key | Example | Meaning |
|-----|---------|---------|
| `repo_version` | `7` | Version number of this file in its repo. |
| `update_url` | `https://example.org/vidya/vedabase-en/repo.json` | Where to look for updates: the URL of `repo.json`. |
| `recommended_min_reader_version` | `2.0.14` | Present only when set (§5). |
| `required_min_reader_version` | `2.1.0` | Present only when set (§5). |
| `modified_at` | `2026-11-22T18:37:53Z` | Time the version was added. |

The legacy key `update-url` is removed. All other keys are kept as they are.

Because these keys are part of the stored file, each diff also carries them. After a diff is applied, the installed file has the new `repo_version`, `update_url` and reader versions. **A patched file has the same schema and data as the full file of that version.** Its bytes differ, because SQLite lays pages out differently, so its whole-file hash differs too.

The reader must not change these keys, or anything else in the library file. Diffs assume the file is exactly the previous version.

---

## 5. Minimum reader versions

Reader versions are dotted numbers (`2`, `2.0`, `2.0.14`, up to four parts), compared numerically: missing parts count as `0` and a leading `v` is ignored.

| Key | If the reader is older than this |
|-----|-----------------------------------|
| `recommended_min_reader_version` | The reader tells the user **before updating** that a newer reader is recommended. The user picks one of three: **update anyway**, **upgrade the reader first** (and update later), or **cancel** (keep the current file). |
| `required_min_reader_version` | The reader **must not apply** this version. It tells the user that the reader has to be upgraded first. Earlier versions in the chain may still be applied (§7). |

Both are set per version with `vidya_repo.py add --recommended-reader / --required-reader`. A value given on the command line wins. Otherwise the value from the input file's `db_meta` is used, and if that is missing too, the previous version's value carries over. `--clear-…` removes a value. The tool warns if the required version goes down.

The reader should also check both keys in `db_meta` when it **opens** a library, for example one the user downloaded and added by hand, and show the same messages.

---

## 6. Diff files and guards

A diff is the output of `vidya_sql_diff.py ORIGINAL NEW`, with two **version guards** added, in a `.zst` file (§2):

```sql
PRAGMA foreign_keys = OFF;
BEGIN;

DROP TABLE IF EXISTS temp._vidya_repo_guard;
CREATE TEMP TABLE _vidya_repo_guard (
  stage TEXT NOT NULL,
  ok INTEGER NOT NULL,
  CONSTRAINT start_version_mismatch CHECK (stage <> 'start' OR ok = 1),
  CONSTRAINT end_version_mismatch CHECK (stage <> 'end' OR ok = 1)
);
-- vidya repo guard: repo_version must be 6 at the start
INSERT OR ROLLBACK INTO temp._vidya_repo_guard (stage, ok) SELECT 'start', COUNT(*) FROM db_meta
  WHERE key = 'repo_version' AND trim(value, '"') = '6';

… schema and row changes from vidya_sql_diff.py …

-- vidya repo guard: repo_version must be 7 at the end
INSERT OR ROLLBACK INTO temp._vidya_repo_guard (stage, ok) SELECT 'end', COUNT(*) FROM db_meta
  WHERE key = 'repo_version' AND trim(value, '"') = '7';
DROP TABLE temp._vidya_repo_guard;

COMMIT;
PRAGMA foreign_keys = ON;
```

- The whole diff is one transaction. It applies completely or not at all.
- If the file is not at the start version, the first guard fails with `CHECK constraint failed: start_version_mismatch` and the transaction is rolled back (`OR ROLLBACK`). Applying a diff twice, or to the wrong file, therefore does nothing.
- If the end state is wrong, the error is `end_version_mismatch`, and again everything is rolled back.
- Apply the script with a function that runs several statements (`sqlite3_exec`, rusqlite `execute_batch`). If it returns an error and the connection is still in a transaction, run `ROLLBACK`. Afterwards re-enable foreign keys (`PRAGMA foreign_keys = ON`) if the connection uses them.

---

## 7. How a reader updates

```
local   = db_meta.repo_version   (integer)
url     = db_meta.update_url
```

1. If `local` or `url` is missing, there is no automatic update. The user can download the full file by hand.
2. Download `url`, bypassing caches (`Cache-Control: no-cache`). If `format` is not `1`, stop and ask the user to upgrade the reader.
3. If `local >= latest`, the library is up to date.
4. **Plan.** For `v = local+1 … latest`:
   - If version `v` has no `diff`, or its `diff.from != v-1`, stop the chain here (*missing diff*).
   - If the reader is older than `v`'s `required_min_reader_version`, stop the chain here (*reader upgrade required*).
   - Otherwise add `v` to the steps.
5. **Ask before running.** If the reader is older than any `recommended_min_reader_version` among the steps, show the recommendation dialog (§5) first. If the chain stops early, also say what happens:
   - *reader upgrade required:* "You can update to version T now; version V needs reader X or newer."
   - *missing diff:* "To get version `latest`, download the full file", linking to `latest`'s `full.file` and saying to install it as `install_file_name`.
6. **Apply.** For each step:
   1. Download `diff.file`.
   2. Check that its SHA-256 equals `diff.sha256`, then decompress it.
   3. Check that the unpacked bytes match `diff.raw_sha256`.
   4. Close other connections to the library and apply the SQL (§6).
   5. Check that `db_meta.repo_version` now equals the step's version.

   Each step is committed on its own. If the reader stops partway (no network, app closed, an error), the file is at a valid version and the next check continues from there. A reader may download all steps before applying the first, so it is not left halfway when offline.
7. A reader several versions behind repeats step 6 for every version in turn. Example: at version 3 with latest 7, it applies `3→4`, `4→5`, `5→6`, `6→7`.

If anything fails (checksum, guard, SQL error), the reader stops, keeps the file at the last good version and reports the problem. There is no automatic full download: the user downloads the latest full file by hand if needed.

`vidya_repo.py update FILE` is a reference client that does exactly this (in `vidya_lib/repo.py`: `plan_update`, `decode_download`, `apply_patch`).

---

## 8. Integrity

| What | How |
|------|-----|
| Published files | `sha256` of the published file and `raw_sha256` of the unpacked content, both in `repo.json`. |
| Applying a diff | Version guards and a single transaction (§6), then a check of `repo_version`. |
| Result of a diff | Checked when the version is added: `add` applies the new diff to a copy of the previous version and compares it with the new file (schema and every row, via `vidya_sql_diff.py`). |
| Whole repo | `verify` checks every listed file and checksum, the version numbering and the diff links. `verify --deep` also replays all diffs from the oldest usable full file and compares the result with the latest full file. |
| Full download | Byte-for-byte: SHA-256 of the unpacked file equals `full.raw_sha256`. |

A patched file cannot be checked against `full.raw_sha256`, because its bytes differ (§4). Its correctness comes from the checks above.

Transport security is HTTPS. There are no signatures.

---

## 9. Publishing

Upload the contents of `REPO/public/` so that the new files arrive **before** `repo.json`:

1. Upload the new `*.vidyapack` and `*.sql.zst` files.
2. Upload `repo.json` last.
3. After `prune`, delete the removed files from the server **after** uploading the new `repo.json`.

That way `repo.json` never points to a file that is not there yet. With rsync, two passes do this:

```bash
rsync -av --exclude repo.json REPO/public/ server:/var/www/vidya/vedabase-en/
rsync -av --delete REPO/public/ server:/var/www/vidya/vedabase-en/
```

Recommended server settings:
- `repo.json`: `Cache-Control: no-cache` (or a short max-age).
- Other files: long caching (`Cache-Control: public, max-age=31536000, immutable`). Their names never get new content.
- Serve `.vidyapack` and `.zst` files as `application/zstd` (or `application/octet-stream`). Do not enable transparent compression for them; they are already compressed.

---

## 10. Removing old files (`prune`)

- **Full files** of old versions can be removed at any time. Updates only need diffs, and the latest full file is always kept. Keeping only the first and the latest full file is fine.
- **Diffs** can be removed too, but readers at versions before a removed diff can no longer update automatically. They must download the latest full file. `prune` lists those versions.
- `prune` writes `repo.json` first and deletes files after, so the manifest never points to a missing file.
- Version entries stay in `repo.json` with `full` / `diff` set to `null`. Version numbers are never reused.

---

## 11. Rules and edge cases

| Case | Behaviour |
|------|-----------|
| New file has the same content as the latest version | `add` refuses it (only repo-managed `db_meta` keys differ). `--force` adds it anyway. |
| Schema change (new or changed table, index, FTS) | Handled by `vidya_sql_diff.py`: a changed table is dropped, recreated and refilled. Set `required_min_reader_version` if older readers cannot read the new schema. |
| Large diff | `add` warns when the diff is more than half the size of the full file. Readers still apply it. Users can always download the full file instead. |
| Broken release | Publish a fixed version. Versions are never edited or deleted from `repo.json`. |
| File without `repo_version` (installed before the repo existed) | No automatic update. The user installs the latest full file. |
| Two tool runs at the same time | The second stops: the repo is locked (`REPO/.lock`). If a crash left the lock behind, delete it. |
| Interrupted `add` | `repo.json` is written last, so the repo stays at the previous version. Leftover `*.tmp` files are cleaned up on the next run, and a half-written version is simply written again. |

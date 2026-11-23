# vidya-tools

Command-line utilities for maintaining [`.vidya`](VIDYA-FORMAT-AGENTS.md) libraries (SQLite databases used by Vidya Editor). Each tool is a standalone Python script for **advanced** workflows—merge prep, bulk fixes—not everyday editing in the app.

**Requirements:** Python 3.9+. Everything is stdlib only, except `vidya_repo.py` on Python older than 3.14: it needs the `zstandard` package (`pip install -r requirements.txt`). Python 3.14+ has zstd built in.

**Format reference:** [VIDYA-FORMAT-AGENTS.md](VIDYA-FORMAT-AGENTS.md)

---

## Scripts

### `vidya_enable_quick_nav.py`

Enables quick navigation (the ⚡ dialog in the reader). Builds a `quick_nav` table from `content_tree` and sets `db_meta.quick_nav = 'true'`. Each book, chapter and verse becomes an entry whose `code` is its slug (or the last `url_paths` segment in legacy files). Entries are nested under their nearest included ancestor and link to `/{type}/{item_id}`. Running it again rebuilds the table.

| Option | Meaning |
|--------|---------|
| `--types a,b,c` | `content_tree` types to include (default `book,chapter,verse`) |
| `--dry-run` | Row counts and a sample only; no backup, no writes |

**Safety:** On a real run, creates a timestamped backup (`name.vidya.bak-YYYYMMDD-HHMMSS`) first.

```bash
python3 vidya_enable_quick_nav.py my-library.vidya --dry-run
python3 vidya_enable_quick_nav.py my-library.vidya
```

---

### `vidya_set_library_title.py`

Shows or changes the library title (`db_meta.library_title`). Run without a title to print the current one. When changing it, also updates `db_meta.modified_at`.

| Option | Meaning |
|--------|---------|
| `TITLE` | New title (positional, optional; omit to print the current title) |
| `--dry-run` | Show current and new title only; no backup, no writes |

**Safety:** On a real run, creates a timestamped backup (`name.vidya.bak-YYYYMMDD-HHMMSS`) first. Does nothing if the title is unchanged.

```bash
python3 vidya_set_library_title.py my-library.vidya
python3 vidya_set_library_title.py my-library.vidya "Vedabase English" --dry-run
python3 vidya_set_library_title.py my-library.vidya "Vedabase English"
```

---

### `vidya_sql_diff.py`

Writes an SQL patch that turns one `.vidya` file into another: apply it to **ORIGINAL** and it becomes identical to **NEW**. Neither input is modified.

The patch covers schema (tables, FTS virtual tables, indexes, views, triggers) and data (row-level `DELETE` / `UPDATE` / `INSERT`, matched by primary key, or `rowid` for tables without one and for FTS tables). A table whose `CREATE` statement changed is dropped, recreated and refilled. FTS shadow tables are skipped; the FTS table itself is diffed, so the index stays consistent. Also syncs `sqlite_sequence` and `PRAGMA user_version`. Values are compared exactly, including type.

| Option | Meaning |
|--------|---------|
| `ORIGINAL NEW` | The file the patch will be applied to, and the file it should become |
| `-o FILE` | Write the patch to `FILE` (default: stdout) |
| `--exclude TABLE` | Leave a table out of the patch (repeatable), e.g. `--exclude search_index` |
| `--verify` | Apply the patch to a temporary copy of ORIGINAL and check it matches NEW |

**Safety:** The patch runs in a single transaction with foreign keys off, so it either applies fully or not at all. Rows whose unique columns change (e.g. swapped slugs) are deleted and re-inserted, so they can't collide halfway through.

```bash
python3 vidya_sql_diff.py original.vidya new.vidya -o patch.sql --verify
sqlite3 original.vidya < patch.sql
```

---

### `vidya_text_diff.py`

Shows the differences between two `.vidya` files in a readable form, like `diff`: for every changed row, each changed column is shown as old → new. Small edits are marked inline, word by word (`[-removed-]{+added+}`, or red/green in a terminal), with long unchanged text shortened to `…`. Text that was rewritten rather than edited (e.g. a translation) is shown as a whole `- old` / `+ new` pair. Added and removed rows are listed with their values. Neither input is modified.

With `--html` it writes a single self-contained HTML page instead, to open in any browser (no internet needed). Removed text is red with strikethrough and added text is green. Rewritten text is shown old and new side by side. There is a filter box (path, id or text), checkboxes for changed / added / removed rows, collapsible tables, and a click on `…` shows the hidden unchanged text (or tick **full text** to show all of it). It follows the system light / dark mode.

Each row is labelled with its place in the library (the nearest `url_path` above it in `content_tree`, e.g. `/bg/2/13`), so changes can be found in the reader. Rows are matched by primary key, as in `vidya_sql_diff.py`. FTS tables repeat the records text and are skipped by default. Long BLOB / base64 values are only summarised by size. Exit code is `0` if the files are the same, `1` if they differ.

| Option | Meaning |
|--------|---------|
| `OLD NEW` | The two files to compare |
| `-o FILE` | Write the report to `FILE` (default: stdout) |
| `--html` | Write a self-contained HTML page instead of text (use with `-o changes.html`) |
| `--table TABLE` | Only compare this table (repeatable), e.g. `--table records` |
| `--exclude TABLE` | Skip a table (repeatable) |
| `--include-fts` | Also compare FTS search tables |
| `--context N` | Words of unchanged text kept around each change (default 8; `-1` = whole text) |
| `--lines` | Show changed text as whole `- old` / `+ new` lines instead of inline marks |
| `--min-similarity R` | Below this similarity (0–1) show `- old` / `+ new` instead of inline marks (default 0.5) |
| `--max-rows N` | Show at most N changed / added / removed rows per table (counts stay exact) |
| `--width N` | Text output: cut long values to N characters (default 200; `0` = no limit) |
| `--no-locate` | Don't look up each row's `url_path` |
| `--color auto\|always\|never` | Text output: colors (default: only when writing to a terminal) |

```bash
python3 vidya_text_diff.py old.vidya new.vidya | less -R
python3 vidya_text_diff.py old.vidya new.vidya --table records -o changes.txt
python3 vidya_text_diff.py old.vidya new.vidya --html -o changes.html && open changes.html
```

Example output:

```
~ records 39 /en/library/bg/introduction … (paragraph)
    content: O my dear Kṛṣṇa{+, ocean of mercy+}, You are the friend of the distressed…
```

---

### `vidya_shift_item_ids.py`

Adds a constant to **global item ids** across the database (entity primary keys, `content_tree`, `url_paths`, parent links, image assets, author links, FTS `item_id`, and id-based wikilinks in record HTML). Use when merging libraries or reserving an id range so imported content does not collide with existing ids.

| Option | Meaning |
|--------|---------|
| `--after-id N` | Shift every id with **`id > N`** (`N = 0` → all ids with `id > 0`) |
| `--start-id N` | Shift every id with **`id >= N`** (`N = 0` → all ids with `id > 0`) |
| `--id-offset K` | Each selected id becomes **`id + K`** (required) |
| `--dry-run` | Collision check and row counts only; no backup, no writes |

Exactly one of `--after-id` or `--start-id` is required.

**Safety:** On a real run, creates a timestamped backup next to the file (`name.vidya.bak-YYYYMMDD-HHMMSS`) before changing anything. Aborts without modifying the database if the shift would overwrite an id that is **not** part of the shift set. Updates `editor_next_item_id` and `modified_at` in `db_meta` when done.

**Examples:**

```bash
# 501, 502, … become 701, 702, … (500 unchanged)
python3 vidya_shift_item_ids.py my-library.vidya --after-id 500 --id-offset 200

# 500, 501, … become 700, 701, …
python3 vidya_shift_item_ids.py my-library.vidya --start-id 500 --id-offset 200

# Preview
python3 vidya_shift_item_ids.py my-library.vidya --after-id 500 --id-offset 200 --dry-run
```

---

### `vidya_repo.py`

Creates and maintains a **vidya repo**: a folder of static files holding every version of one `.vidya` library, plus an SQL diff between each pair of consecutive versions. Upload `REPO/public/` to a web server, and a reader app updates installed libraries from `db_meta.update_url`, applying the diffs one after another. Repo format and reader rules: [VIDYA-REPO-FORMAT.md](VIDYA-REPO-FORMAT.md).

When a file is added, the tool copies it into the repo and writes `repo_version`, `update_url` (and the min reader versions) into the **copy**. The input file is never changed. Diffs are made with `vidya_sql_diff.py`, wrapped in version guards, and test-applied before they are published.

| Command | What it does |
|---------|--------------|
| `init REPO FILE --slug SLUG --base-url URL` | Create the repo with `FILE` as version 1. `update_url` becomes `URL/repo.json`. Optional `--title`. |
| `add REPO FILE` | Add `FILE` as the next version, with a diff from the previous one. Refuses a file whose content did not change (`--force` to add it anyway). `--skip-verify` skips the test-apply. |
| `log REPO` | List versions: sizes, min reader versions, labels. |
| `verify REPO [--deep]` | Check every file and checksum against `repo.json`. `--deep` also replays all diffs and compares the result with the latest full file. |
| `prune REPO --full 2-5 [--diff 2-3]` | Delete old full files and/or the diffs leading **to** the given versions. Never the latest full file. Warns which versions can no longer update automatically. |
| `rsync REPO DEST [--delete]` | Upload `REPO/public/` to `DEST` (e.g. `server:/var/www/vidya/vedabase-en`) in a safe order: runs `verify`, uploads the new files, then `repo.json`. `--delete` then removes files on `DEST` that are no longer in `public/` (after `prune`). `--dry-run` uploads nothing, `--skip-verify` skips the check, `--rsync-arg ARG` (repeatable) passes extra options to rsync. |
| `update FILE [--repo-url URL] [--reader-version V]` | Reference reader client: updates an installed `.vidya` file from its repo, as a reader app should. `--yes` updates even if a newer reader is recommended. `--dry-run` shows the plan only. |

Options for `init` and `add`:

| Option | Meaning |
|--------|---------|
| `--label TEXT`, `--notes TEXT` | Human name and release notes for the version |
| `--recommended-reader V` / `--clear-recommended-reader` | Set / remove `recommended_min_reader_version` (e.g. `2.0.14`) |
| `--required-reader V` / `--clear-required-reader` | Set / remove `required_min_reader_version` |
| `--dry-run` | Show what would happen; write nothing |

Without these options, a min reader version is taken from the input file's `db_meta`, or else carried over from the previous version.

**Safety:** Input files are never modified. `repo.json` is written last (a failed run leaves the repo as it was), the repo is locked while a command runs, and every published file has SHA-256 checksums in `repo.json`.

```bash
python3 vidya_repo.py init repos/vedabase-en vedabase-en.vidya \
    --slug vedabase-en --base-url https://example.org/vidya/vedabase-en/
python3 vidya_repo.py add repos/vedabase-en vedabase-en.vidya --label "November corrections" --recommended-reader 2.0.14
python3 vidya_repo.py log repos/vedabase-en
python3 vidya_repo.py verify repos/vedabase-en --deep
python3 vidya_repo.py prune repos/vedabase-en --full 2-5 --dry-run

# upload: new files first, repo.json last (--delete also removes pruned files)
python3 vidya_repo.py rsync repos/vedabase-en server:/var/www/vidya/vedabase-en --delete

# check from a reader's point of view
python3 vidya_repo.py update ~/Library/vedabase-en.vidya --reader-version 2.0.14 --dry-run
```

Tests: `python3 -m unittest discover tests -v`

---

## Shared code

`vidya_lib/` holds helpers used by the scripts (id tables, collision checks, SQLite helpers). It is not run directly.

---

## Author & License

Developed by **Prahlad Nrsimha das** (Petr Vacha) at **Get Bhakti s.r.o.**  
Licensed under the [MIT License](LICENSE).
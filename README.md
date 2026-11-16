# vidya-tools

Command-line utilities for maintaining [`.vidya`](VIDYA-FORMAT-AGENTS.md) libraries (SQLite databases used by Vidya Editor). Each tool is a standalone Python script for **advanced** workflows—merge prep, bulk fixes—not everyday editing in the app.

**Requirements:** Python 3.9+ (stdlib only; no pip packages).

**Format reference:** [VIDYA-FORMAT-AGENTS.md](VIDYA-FORMAT-AGENTS.md)

---

## Scripts

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

## Shared code

`vidya_lib/` holds helpers used by the scripts (id tables, collision checks, SQLite helpers). It is not run directly.

---

## Author & License

Developed by **Prahlad Nrsimha das** (Petr Vacha) at **Get Bhakti s.r.o.**  
Licensed under the [MIT License](LICENSE).
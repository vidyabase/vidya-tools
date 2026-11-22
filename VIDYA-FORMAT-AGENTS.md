# Vidya Format — Complete Specification for Building `.vidya` Files

**Self-contained specification** for humans and AI agents that **create, convert to, or read** `.vidya` files from any source (Markdown folders, HTML, JSON, CMS exports, legacy `vidya-builder` output, etc.).

Everything required to produce a correct `.vidya` file with **all supported content types, images, tables, media, search, and navigation** is in this document. No other files are required.

**Output:** a single SQLite 3 file with extension `.vidya`, UTF-8 text in SQL string columns, readable by Vidya Editor and compatible tools.

**Format version:** `2` (`db_meta.vidya_format_version`).

---

## Table of contents

1. [Overview](#1-overview)
2. [Master reference — all node and content types](#2-master-reference--all-node-and-content-types)
3. [Content type catalog (exact storage)](#3-content-type-catalog-exact-storage)
4. [Container types (exact storage)](#4-container-types-exact-storage)
5. [Converter pipeline](#5-converter-pipeline)
6. [Complete database schema](#6-complete-database-schema)
7. [Library metadata (`db_meta`)](#7-library-metadata-db_meta)
8. [Global ID allocation](#8-global-id-allocation)
9. [URL paths](#9-url-paths)
10. [Slug normalization](#10-slug-normalization)
11. [Images and book covers](#11-images-and-book-covers)
12. [Full-text search](#12-full-text-search)
13. [Authors](#13-authors)
14. [Wikilinks](#14-wikilinks)
15. [Legacy / `vidya-builder` compatibility](#15-legacy--vidya-builder-compatibility)
16. [Converting HTML](#16-converting-html)
17. [Converting Markdown](#17-converting-markdown)
18. [Media URL detection](#18-media-url-detection)
19. [Editing existing files](#19-editing-existing-files)
20. [Validation checklist](#20-validation-checklist)
21. [Worked examples](#21-worked-examples)
22. [Common mistakes](#22-common-mistakes)
23. [Agent task checklist](#23-agent-task-checklist)

---

## 1. Overview

A `.vidya` file is a **structured library** stored as SQLite:

```
.vidya
├── db_meta                 settings + next free item_id
├── content_tree            parent/child tree + sort order (THE SPINE)
├── url_paths               slash-joined slug chains (navigation + search)
├── books … letters         hierarchy entities (titles, metadata)
├── records                 text / JSON content blocks
├── image_assets            reusable image blobs (not tree nodes)
├── images                  inline image placements
├── authors, author_links   optional metadata
└── search_index            optional FTS5 virtual table
```

### Invariants

| Rule | Detail |
|------|--------|
| Spine | Every library item has exactly one `content_tree` row. |
| Stable ID | `content_tree.item_id` = PK in the entity table. **Never renumber or reuse.** |
| Parent | `content_tree.parent_id` → parent's **`item_id`** (not `content_tree.id`). `NULL` = root. |
| Global IDs | One integer sequence across all entity tables (§8). |
| Order | `sort_order` — 0-based; containers and records share one sequence per parent. |
| Hierarchy mirror | Container entity tables duplicate `parent_id` from `content_tree`. |
| Records parent | `records` and `images` have **no** `parent_id` — only `content_tree.parent_id`. |
| Slugs | Optional; empty → SQL `NULL`, never `''`. |
| `is_page` | `1` = container (reader page); `0` = record / image block. |

### Pragmas (set on every open/create)

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;
```

---

## 2. Master reference — all node and content types

### 2.1 Record / block content types (19 types)

These are **leaf content blocks** — children of a container (chapter, verse, section, …).

| # | `records.content_type` | `content_tree.type` | Entity table | `content` format |
|---|------------------------|---------------------|--------------|------------------|
| 1 | `paragraph` | `record` | `records` | HTML |
| 2 | `paragraph-centered` | `record` | `records` | HTML |
| 3 | `verse-text` | `record` | `records` | HTML |
| 4 | `translation` | `record` | `records` | HTML |
| 5 | `reference` | `record` | `records` | HTML |
| 6 | `sub-chapter` | `record` | `records` | HTML |
| 7 | `small-heading` | `record` | `records` | HTML |
| 8 | `footnote` | `record` | `records` | HTML |
| 9 | `bullet-list` | `record` | `records` | HTML |
| 10 | `numbered-list` | `record` | `records` | HTML |
| 11 | `quote` | `record` | `records` | HTML |
| 12 | `audio` | `record` | `records` | JSON |
| 13 | `video` | `record` | `records` | JSON |
| 14 | `file-link` | `record` | `records` | JSON |
| 15 | `devanagari` | `devanagari` | `records` | HTML / plain |
| 16 | `bengali` | `bengali` | `records` | HTML / plain |
| 17 | `wfw-text` | `record` | `records` | HTML |
| 18 | `wfw-pairs` | `wfw-pairs` | `records` | JSON array |
| 19 | `table` | `table` | `records` | JSON object |
| 20 | _(image block)_ | `image` | `images` + `image_assets` | see §3.20 — **not** in `records` |

**Naming:** always **hyphenated** (`verse-text`, not `verse_text`).

For types 1–14 and `wfw-text`: `content_tree.type = 'record'` and the style is in `records.content_type`.

For `devanagari`, `bengali`, `wfw-pairs`, and `table`: `content_tree.type` **equals** `records.content_type`.

For type 20: no `records` row — data lives in `images` (+ optional `image_assets`).

### 2.2 Container types (8 types)

| # | `content_tree.type` | Entity table | `is_page` |
|---|---------------------|--------------|-----------|
| 1 | `book` | `books` (`books.type='book'`) | 1 |
| 2 | `canto` | `books` (`books.type='canto'`) | 1 |
| 3 | `chapter` | `chapters` | 1 |
| 4 | `verse` | `verses` | 1 |
| 5 | `section` | `sections` | 1 |
| 6 | `footnote-container` | `footnote_containers` | 1 |
| 7 | `transcript` | `transcripts` | 1 |
| 8 | `letter` | `letters` | 1 |

Any container may contain any other container or any record type. No schema enforces a hierarchy template.

Typical spiritual-text shape:

```
book → canto → chapter → verse → [record blocks]
                              └── footnote-container → [footnote records]
```

---

## 3. Content type catalog (exact storage)

Every record block requires **two rows**: one in `records` (except `image`) and one in `content_tree`.

**Common `content_tree` pattern for records:**

```sql
INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (<parent_item_id>, '<tree-type>', <item_id>, NULL, 0, <sort_order>);
```

- `parent_id` = container's `item_id`
- `slug` = `NULL` for records (almost always)
- `is_page` = `0`
- `sort_order` = 0-based position among siblings

**Allowed HTML inline tags (writers):** `<strong>`, `<em>`, `<u>`, `<br>`, plus `<a>` for wikilinks (§14). List types additionally use `<ul>/<ol>/<li>` (§3.9, §3.10).

**No block wrappers (writers).** Record `content` is inline HTML only — writers must **not** wrap in `<p>`, `<div>`, or other block elements. Each `records` row is already one block; the `content_type` controls presentation (centering, bold, etc.).

**Readers tolerate `<p>`.** This is a rule for *producers*; files in the wild (legacy `vidya-builder` output and the example in §3.14 below) do contain `<p>…</p>` wrappers, occasional `<div>`, `<b>/<i>`, and raw `<table>` HTML. A reader must accept them: keep `<p>` in the sanitizer allow-list, render it with zero margins inside the record block, and strip the outer `<p>` only where it changes layout (`devanagari`, `bengali`). See AGENT_INSTRUCTIONS.md §8.4.

**Types that must keep internal `<br>`** (do not split into separate blocks): `verse-text`, `devanagari`, `bengali`, `translation`.

---

### 3.1 `paragraph`

| Field | Value |
|-------|-------|
| `records.content_type` | `paragraph` |
| `content_tree.type` | `record` |
| `content` | Inline HTML |

```sql
INSERT INTO records (id, content_type, content)
VALUES (101, 'paragraph', 'The soul is eternal and indestructible.');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 101, NULL, 0, 0);
```

Empty: `""`

---

### 3.2 `paragraph-centered`

| Field | Value |
|-------|-------|
| `records.content_type` | `paragraph-centered` |
| `content_tree.type` | `record` |
| `content` | Inline HTML (centering comes from `content_type`) |

```sql
INSERT INTO records (id, content_type, content)
VALUES (102, 'paragraph-centered', 'Centered text here.');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 102, NULL, 0, 1);
```

---

### 3.3 `verse-text`

| Field | Value |
|-------|-------|
| `records.content_type` | `verse-text` |
| `content_tree.type` | `record` |
| `content` | Inline HTML — typically italic (`<em>`), `<br>` between lines |

```sql
INSERT INTO records (id, content_type, content)
VALUES (103, 'verse-text',
  '<em>dehino ’smin yathā dehe<br>kaumāraṁ yauvanaṁ jarā</em>');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 103, NULL, 0, 2);
```

---

### 3.4 `translation`

| Field | Value |
|-------|-------|
| `records.content_type` | `translation` |
| `content_tree.type` | `record` |
| `content` | HTML — bold prose (typically `<strong>`) |

```sql
INSERT INTO records (id, content_type, content)
VALUES (104, 'translation',
  '<strong>As the embodied soul continually passes, in this body, from boyhood to youth to old age, the soul similarly passes into another body at death.</strong>');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 104, NULL, 0, 3);
```

---

### 3.5 `reference`

| Field | Value |
|-------|-------|
| `records.content_type` | `reference` |
| `content_tree.type` | `record` |
| `content` | HTML — right-aligned citation |

```sql
INSERT INTO records (id, content_type, content)
VALUES (105, 'reference',
  'Bg 2.13 — Purport');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 105, NULL, 0, 4);
```

---

### 3.6 `sub-chapter`

| Field | Value |
|-------|-------|
| `records.content_type` | `sub-chapter` |
| `content_tree.type` | `record` |
| `content` | HTML — heading-style bold text |

```sql
INSERT INTO records (id, content_type, content)
VALUES (106, 'sub-chapter',
  '<strong>The Eternal Soul</strong>');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 106, NULL, 0, 5);
```

---

### 3.7 `small-heading`

| Field | Value |
|-------|-------|
| `records.content_type` | `small-heading` |
| `content_tree.type` | `record` |
| `content` | HTML — minor heading |

```sql
INSERT INTO records (id, content_type, content)
VALUES (107, 'small-heading',
  '<strong>Verse summary</strong>');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 107, NULL, 0, 6);
```

---

### 3.8 `footnote`

| Field | Value |
|-------|-------|
| `records.content_type` | `footnote` |
| `content_tree.type` | `record` |
| `content` | HTML — footnote body |

Typically placed inside a `footnote-container` child of the verse/chapter.

```sql
INSERT INTO records (id, content_type, content)
VALUES (108, 'footnote',
  'See also commentary on Bg 2.12.');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (60, 'record', 108, NULL, 0, 0);
-- parent_id 60 = footnote-container item_id
```

---

### 3.9 `bullet-list`

| Field | Value |
|-------|-------|
| `records.content_type` | `bullet-list` |
| `content_tree.type` | `record` |
| `content` | HTML `<ul><li>…</li></ul>` |

```sql
INSERT INTO records (id, content_type, content)
VALUES (109, 'bullet-list',
  '<ul><li>First item</li><li>Second item</li></ul>');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 109, NULL, 0, 7);
```

Empty: `<ul><li></li></ul>`

---

### 3.10 `numbered-list`

| Field | Value |
|-------|-------|
| `records.content_type` | `numbered-list` |
| `content_tree.type` | `record` |
| `content` | HTML `<ol><li>…</li></ol>` |

```sql
INSERT INTO records (id, content_type, content)
VALUES (110, 'numbered-list',
  '<ol><li>First step</li><li>Second step</li></ol>');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 110, NULL, 0, 8);
```

Empty: `<ol><li></li></ol>`

---

### 3.11 `quote`

| Field | Value |
|-------|-------|
| `records.content_type` | `quote` |
| `content_tree.type` | `record` |
| `content` | HTML — italic quoted text |

```sql
INSERT INTO records (id, content_type, content)
VALUES (111, 'quote',
  '<em>For one who has taken birth, death is certain.</em>');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 111, NULL, 0, 9);
```

---

### 3.12 `devanagari`

| Field | Value |
|-------|-------|
| `records.content_type` | `devanagari` |
| `content_tree.type` | `devanagari` |
| `content` | Plain text or inline HTML; `<br>` allowed |

```sql
INSERT INTO records (id, content_type, content)
VALUES (112, 'devanagari',
  'धृतराष्ट्र उवाच<br>धर्मक्षेत्रे कुरुक्षेत्रे');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'devanagari', 112, NULL, 0, 10);
```

Note: `content_tree.type` is `devanagari`, **not** `record`.

---

### 3.13 `bengali`

| Field | Value |
|-------|-------|
| `records.content_type` | `bengali` |
| `content_tree.type` | `bengali` |
| `content` | Plain text or inline HTML; `<br>` allowed |

```sql
INSERT INTO records (id, content_type, content)
VALUES (113, 'bengali',
  'বাংলা পাঠ এখানে');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'bengali', 113, NULL, 0, 11);
```

---

### 3.14 `wfw-text` (word-for-word text)

| Field | Value |
|-------|-------|
| `records.content_type` | `wfw-text` |
| `content_tree.type` | `record` |
| `content` | HTML (same editing model as `paragraph`) |

```sql
INSERT INTO records (id, content_type, content)
VALUES (114, 'wfw-text',
  '<em>dharma-kṣetre</em> — in the place of pilgrimage; kuru-kṣetre — at Kurukshetra');
-- Note: older files store this with a <p>…</p> wrapper; readers must accept both (§3 "Readers tolerate <p>").

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 114, NULL, 0, 12);
```

---

### 3.15 `wfw-pairs` (word-for-word pairs)

| Field | Value |
|-------|-------|
| `records.content_type` | `wfw-pairs` |
| `content_tree.type` | `wfw-pairs` |
| `content` | JSON **array** of `{word, translation}` objects |

Each field is a string — plain text or inline HTML fragment.

```sql
INSERT INTO records (id, content_type, content)
VALUES (114, 'wfw-pairs', '[
  {"word": "dharma-kṣetre", "translation": "in the place of pilgrimage"},
  {"word": "kuru-kṣetre", "translation": "at Kurukshetra"},
  {"word": "samavetā", "translation": "assembled"}
]');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'wfw-pairs', 114, NULL, 0, 12);
```

Empty: `[]`

**JSON rules:**
- Top level must be an array, not an object.
- Each element must have `word` and `translation` string keys.
- HTML in fields is allowed: `"word": "dharma"`.

---

### 3.16 `table`

| Field | Value |
|-------|-------|
| `records.content_type` | `table` |
| `content_tree.type` | `table` |
| `content` | JSON **object** with `columns` and `rows` |

#### JSON schema

```json
{
  "columns": [
    { "id": "c1" },
    { "id": "c2" }
  ],
  "rows": [
    {
      "headerRow": true,
      "cells": {
        "c1": { "content": "Word" },
        "c2": { "content": "Translation" }
      }
    },
    {
      "cells": {
        "c1": { "content": "dharma" },
        "c2": { "content": "religion, duty" }
      }
    },
    {
      "cells": {
        "c1": { "content": "merged header", "colspan": 2 }
      }
    }
  ],
  "caption": "Word-for-word glossary"
}
```

| JSON field | Required | Rules |
|------------|----------|-------|
| `columns` | yes | Array of `{ "id": "<unique-string>" }`. Optional `label` is legacy metadata only. |
| `rows` | yes | Array of row objects. |
| `rows[].cells` | yes | Object keyed by column `id`. Every column must appear in each row. |
| `rows[].cells[id].content` | yes | Inline HTML string. Empty cell: `""`. |
| `rows[].cells[id].colspan` | no | Integer ≥ 2. Omit when 1. |
| `rows[].headerRow` | no | `true` marks a header row. |
| `caption` | no | Plain string. |

**Column ids:** generate stable unique strings, e.g. `c` + base36 timestamp: `c1k2j3abc`.

#### Exact SQL example

```sql
INSERT INTO records (id, content_type, content)
VALUES (115, 'table', '{
  "columns": [{"id": "c1"}, {"id": "c2"}],
  "rows": [
    {
      "headerRow": true,
      "cells": {
        "c1": {"content": "Sanskrit"},
        "c2": {"content": "English"}
      }
    },
    {
      "cells": {
        "c1": {"content": "dharma"},
        "c2": {"content": "duty"}
      }
    }
  ],
  "caption": "Key terms"
}');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'table', 115, NULL, 0, 13);
```

#### Default empty table

```json
{"columns":[{"id":"c1"},{"id":"c2"}],"rows":[{"cells":{"c1":{"content":""},"c2":{"content":""}}}]}
```

#### Markdown pipe table → this JSON

Input:

```markdown
| Word | Translation |
|------|-------------|
| dharma | religion |
| karma | action |
```

Steps:
1. Parse header + `|---|` separator + body rows.
2. Assign column ids `c1`, `c2`, …
3. First row → `headerRow: true`, cell content as plain text or inline HTML.
4. Body rows → normal rows with inline HTML in each cell.

---

### 3.17 `audio`

| Field | Value |
|-------|-------|
| `records.content_type` | `audio` |
| `content_tree.type` | `record` |
| `content` | JSON media object |

```sql
INSERT INTO records (id, content_type, content)
VALUES (116, 'audio', '{
  "url": "https://w.soundcloud.com/player/?url=https%3A//api.soundcloud.com/tracks/123",
  "provider": "soundcloud",
  "title": "Lecture on Bg 2.13",
  "caption": "Recorded 1972"
}');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 116, NULL, 0, 14);
```

| JSON field | Required | Values |
|------------|----------|--------|
| `url` | yes | Absolute URL |
| `provider` | no | `file`, `youtube`, `vimeo`, `soundcloud`, `generic` |
| `embedId` | no | Platform id when detectable |
| `title` | no | Display title |
| `caption` | no | Caption text |

Minimum valid: `{"url":""}`. See §18 for auto-detecting `provider` / `embedId`.

---

### 3.18 `video`

| Field | Value |
|-------|-------|
| `records.content_type` | `video` |
| `content_tree.type` | `record` |
| `content` | JSON media object (same schema as audio) |

```sql
INSERT INTO records (id, content_type, content)
VALUES (117, 'video', '{
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "provider": "youtube",
  "embedId": "dQw4w9WgXcQ",
  "title": "Bhagavad-gita overview"
}');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 117, NULL, 0, 15);
```

---

### 3.19 `file-link`

| Field | Value |
|-------|-------|
| `records.content_type` | `file-link` |
| `content_tree.type` | `record` |
| `content` | JSON media object (same schema as audio/video) |

A **link to any external file** (PDF, document, archive, etc.). The file bytes are **not** stored in the database — only the URL and optional metadata. (Human-facing label: **File link**.)

```sql
INSERT INTO records (id, content_type, content)
VALUES (118, 'file-link', '{
  "url": "https://example.com/lecture-notes.pdf",
  "provider": "file",
  "title": "Lecture notes (PDF)",
  "caption": "Supplementary reading"
}');

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'record', 118, NULL, 0, 16);
```

| JSON field | Required | Values |
|------------|----------|--------|
| `url` | yes | Absolute URL to the file or download page |
| `provider` | no | `file` or `generic` (no embed providers) |
| `title` | no | Display title for the link |
| `caption` | no | Caption text |

Minimum valid: `{"url":""}`. Unlike `audio` / `video`, do **not** set `provider` to `youtube`, `vimeo`, or `soundcloud` — use those content types for playable media instead.

---

### 3.20 `image` (inline image block)

**Does not use `records`.** Three tables involved:

| Table | Role |
|-------|------|
| `image_assets` | Binary blob (shared, reusable) |
| `images` | Placement (caption, alt, size, align) — PK = `item_id` |
| `content_tree` | Tree node with `type = 'image'` |

#### Step 1 — `image_assets` (once per unique file)

```sql
INSERT INTO image_assets (id, data, mime, width, height, label, default_alt)
VALUES (
  200,
  <webp_or_svg_bytes>,
  'image/webp',
  640,
  480,
  'Diagram of the soul',
  'Diagram showing transmigration of the soul'
);
```

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | From global id space |
| `data` | BLOB NOT NULL | WebP (raster) or UTF-8 SVG bytes |
| `mime` | TEXT | `image/webp` or `image/svg+xml` |
| `width`, `height` | INTEGER | Pixels after processing |
| `label` | TEXT | Library catalog label (optional) |
| `default_alt` | TEXT | Default alt text (optional) |

#### Step 2 — `images` (one per placement)

```sql
INSERT INTO images (id, asset_id, caption, alt, size, max_height, align)
VALUES (
  201,
  200,
  'Figure 1: The soul changes bodies',
  'Diagram of the soul',
  'full',
  NULL,
  'center'
);
```

| Column | Type | Default | Allowed values |
|--------|------|---------|----------------|
| `id` | INTEGER PK | — | Same as `content_tree.item_id` |
| `asset_id` | INTEGER | NULL | → `image_assets.id`; NULL = no image attached |
| `caption` | TEXT | NULL | Visible caption |
| `alt` | TEXT | NULL | Alt text (falls back to asset `default_alt`) |
| `size` | TEXT | `'full'` | `full`, `half`, `third`, `quarter`, `inline`, `natural`, `custom` |
| `max_height` | INTEGER | NULL | Pixels; **only** when `size = 'custom'` |
| `align` | TEXT | `'center'` | `left`, `center`, `right` |

#### Step 3 — `content_tree`

```sql
INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (50, 'image', 201, NULL, 0, 16);
```

#### JSON equivalent (for reference)

Some tools serialize an image block as JSON when exchanging data. The canonical **database storage** is the `images` table columns above. JSON form:

```json
{
  "assetId": 200,
  "caption": "Figure 1: The soul changes bodies",
  "alt": "Diagram of the soul",
  "size": "full",
  "align": "center"
}
```

When `size` is `custom`, include `"maxHeight": 320` (camelCase in JSON; `max_height` in SQL).

#### Reusing assets

Multiple `images` placements may reference the same `image_assets.id`. Deduplicate identical source files to one asset row.

---

## 4. Container types (exact storage)

Every container requires **two rows**: entity table + `content_tree`.

**Common `content_tree` pattern:**

```sql
INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (<parent_item_id_or_NULL>, '<type>', <item_id>, <slug_or_NULL>, 1, <sort_order>);
```

### 4.1 `book`

```sql
INSERT INTO books (id, title, type, parent_id, has_advanced_view, cover)
VALUES (1, 'Bhagavad-gita As It Is', 'book', NULL, 0, NULL);

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (NULL, 'book', 1, 'bg', 1, 0);
```

Optional: `short_title`, `medium_title`, `cover` (WebP BLOB — §11.4).

### 4.2 `canto`

```sql
INSERT INTO books (id, title, type, parent_id, has_advanced_view)
VALUES (2, 'Canto 1', 'canto', 1, 0);

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (1, 'canto', 2, '1', 1, 0);
```

Optional: `short_title`, `medium_title`, `cover` (WebP BLOB — §11.4).

### 4.3 `chapter`

```sql
INSERT INTO chapters (id, title, chapter_title, parent_id, has_advanced_view)
VALUES (3, 'Chapter 2', 'Contents of the Gita Summarized', 2, 0);

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (2, 'chapter', 3, '2', 1, 0);
```

### 4.4 `verse`

```sql
INSERT INTO verses (id, title, excerpt, parent_id)
VALUES (4, '2.13', 'The soul changes bodies', 3);

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (3, 'verse', 4, '13', 1, 0);
```

### 4.5 `section`

Generic container (introductions, appendices, parts).

```sql
INSERT INTO sections (id, title, parent_id)
VALUES (5, 'Introduction', 1);

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (1, 'section', 5, 'introduction', 1, 1);
```

### 4.6 `footnote-container`

Groups footnote records after main text.

```sql
INSERT INTO footnote_containers (id, title, parent_id)
VALUES (6, 'Footnotes', 4);

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (4, 'footnote-container', 6, NULL, 1, 1);
```

Then add `footnote` records (§3.8) as children of item_id `6`.

### 4.7 `transcript`

```sql
INSERT INTO transcripts (id, title, location, date, parent_id)
VALUES (7, 'Lecture on Bg 2.13', 'Los Angeles', '1968-11-25', NULL);

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (NULL, 'transcript', 7, 'lecture-bg-2-13', 1, 0);
```

### 4.8 `letter`

```sql
INSERT INTO letters (id, title, location, date, parent_id)
VALUES (8, 'Letter to Hayagriva', 'San Francisco', '1968-02-14', NULL);

INSERT INTO content_tree (parent_id, type, item_id, slug, is_page, sort_order)
VALUES (NULL, 'letter', 8, 'letter-hayagriva-1968', 1, 0);
```

`parent_id` on entity tables must mirror `content_tree.parent_id`.

---

## 5. Converter pipeline

```
1.  Plan tree           map source → containers, records, slugs
2.  Create DB           §6 schema + pragmas
3.  Write db_meta       §7
4.  Allocate IDs        §8
5.  Insert containers   §4 top-down
6.  Insert records      §3 in sort_order
7.  Insert images       §3.20 / §11
8.  Build url_paths     §9
9.  Build search_index  §12 (recommended)
10. Validate            §20
```

Run steps 5–9 in one transaction when possible.

---

## 6. Complete database schema

```sql
CREATE TABLE db_meta (
    id    INTEGER PRIMARY KEY,
    key   TEXT UNIQUE NOT NULL,
    value TEXT
);

CREATE TABLE content_tree (
    id         INTEGER PRIMARY KEY,
    parent_id  INTEGER,
    type       TEXT NOT NULL,
    item_id    INTEGER NOT NULL UNIQUE,
    slug       TEXT,
    is_page    INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_content_tree_parent ON content_tree(parent_id, sort_order);
CREATE INDEX idx_content_tree_item   ON content_tree(item_id);

CREATE UNIQUE INDEX idx_content_tree_slug_per_parent
    ON content_tree(parent_id, slug) WHERE slug IS NOT NULL;

CREATE TABLE url_paths (
    url_path TEXT PRIMARY KEY,
    type     TEXT NOT NULL,
    item_id  INTEGER NOT NULL UNIQUE
);

CREATE INDEX idx_url_paths_item ON url_paths(item_id);

CREATE TABLE books (
    id INTEGER PRIMARY KEY, title TEXT, short_title TEXT, medium_title TEXT,
    type TEXT, has_advanced_view INTEGER NOT NULL DEFAULT 0,
    cover BLOB, parent_id INTEGER
);

CREATE TABLE chapters (
    id INTEGER PRIMARY KEY, title TEXT, chapter_title TEXT,
    short_title TEXT, medium_title TEXT,
    has_advanced_view INTEGER NOT NULL DEFAULT 0, parent_id INTEGER
);

CREATE TABLE verses (
    id INTEGER PRIMARY KEY, title TEXT, short_title TEXT, medium_title TEXT,
    excerpt TEXT, parent_id INTEGER
);

CREATE TABLE sections (
    id INTEGER PRIMARY KEY, title TEXT, short_title TEXT, medium_title TEXT,
    parent_id INTEGER
);

CREATE TABLE footnote_containers (
    id INTEGER PRIMARY KEY, title TEXT, short_title TEXT, medium_title TEXT,
    parent_id INTEGER
);

CREATE TABLE transcripts (
    id INTEGER PRIMARY KEY, title TEXT, location TEXT, date TEXT, parent_id INTEGER
);

CREATE TABLE letters (
    id INTEGER PRIMARY KEY, title TEXT, location TEXT, date TEXT, parent_id INTEGER
);

CREATE TABLE records (
    id INTEGER PRIMARY KEY, content_type TEXT NOT NULL, content TEXT NOT NULL
);

CREATE TABLE image_assets (
    id INTEGER PRIMARY KEY, data BLOB NOT NULL,
    mime TEXT NOT NULL DEFAULT 'image/webp',
    width INTEGER, height INTEGER, label TEXT, default_alt TEXT
);

CREATE TABLE images (
    id INTEGER PRIMARY KEY, asset_id INTEGER,
    caption TEXT, alt TEXT,
    size TEXT NOT NULL DEFAULT 'full',
    max_height INTEGER, align TEXT NOT NULL DEFAULT 'center'
);

CREATE INDEX idx_images_asset ON images(asset_id);

CREATE TABLE authors (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, bio TEXT, photo BLOB
);

CREATE TABLE author_links (
    id INTEGER PRIMARY KEY, type TEXT NOT NULL, item_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'author'
);

CREATE INDEX idx_author_links_item   ON author_links(type, item_id);
CREATE INDEX idx_author_links_author ON author_links(author_id);
```

Do **not** add `backbone_id` columns in new files.

---

## 7. Library metadata (`db_meta`)

| key | example | required |
|-----|---------|----------|
| `vidya_format_version` | `2` | yes |
| `editor_version` | `my-tool/1.0.0` | yes |
| `library_title` | `Vedabase English` | recommended |
| `language_code` | `en` | yes |
| `language_name` | `English` | yes |
| `created_at` | `2026-07-18T10:00:00Z` | yes (RFC 3339 UTC) |
| `modified_at` | `2026-07-18T10:00:00Z` | yes |
| `editor_next_item_id` | `42` | yes |
| `fts_enabled` | `1` | yes |
| `fts_built` | `1` | yes after building §12 |

Preserve unknown keys when editing existing files.

---

## 8. Global ID allocation

Shared across: `books`, `chapters`, `verses`, `sections`, `footnote_containers`, `transcripts`, `letters`, `records`, `image_assets`, `images`.

```python
next_id = 1
def allocate_id():
    global next_id
    id = next_id; next_id += 1; return id
# After all inserts: store next_id in db_meta.editor_next_item_id
```

Bootstrap for existing files: `editor_next_item_id = 1 + MAX(id)` across all tables above.

`content_tree.id` is internal autoincrement — never use as FK.

---

## 9. URL paths

`url_paths` maps slash-joined slugs → `item_id`. Used for wikilinks, navigation, and search.

| Column | Example |
|--------|---------|
| `url_path` | `bg/2/13` (no leading `/`) |
| `type` | `verse` |
| `item_id` | `4` |

### Broken chain rule

If a node has no slug, it gets no `url_paths` row and **all descendants are excluded**.

### Compute path

```
if slug is NULL → no path
if parent_prefix is NULL → no path (parent not in url_paths)
if parent_prefix == "" → path = slug          (root node)
else → path = parent_prefix + "/" + slug
```

### Full rebuild (pseudocode)

```
DELETE FROM url_paths
for each root (parent_id IS NULL) in sort_order:
    bfs_rebuild(root, parent_prefix="")

function bfs_rebuild(node, parent_prefix):
    path = compute_path(node.slug, parent_prefix)
    if path:
        INSERT url_paths (url_path, type, item_id)
        for each child ordered by sort_order:
            bfs_rebuild(child, path)
```

---

## 10. Slug normalization

```python
import unicodedata, re
def normalize_slug(s):
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or None  # NULL in SQL
```

On sibling collision: append `_1`, `_2`, …

| Input | Slug |
|-------|------|
| `Bhagavad-gītā` | `bhagavad-gita` |
| `2.13` | `2-13` |

---

## 11. Images and book covers

### 11.1 Raster processing (JPEG, PNG, WebP, GIF)

1. Decode; resize so longest side ≤ **800 px**.
2. Encode **WebP** quality **82**.
3. Store in `image_assets.data` or `books.cover`.
4. Set `mime = 'image/webp'`; record `width`, `height`.

### 11.2 SVG processing

1. Verify UTF-8 and contains `<svg`.
2. **Reject** if lowercase content contains: `<script`, `javascript:`, `onload=`, `onclick=`, `onerror=`, `onmouseover=`, `<foreignobject`, `data:text/html`.
3. Store bytes; `mime = 'image/svg+xml'`.
4. Parse `width`/`height` from `<svg>` attributes or `viewBox`.

### 11.3 Accepted input formats

JPEG, PNG, WebP, GIF (raster); SVG (vector, into `image_assets` only).

### 11.4 Book covers

- Store **only** in `books.cover` (WebP BLOB).
- **Not** in `image_assets`.
- Only on `books` rows where `books.type` is `'book'` or `'canto'`.
- Store raw image bytes as a BLOB — **not** base64 TEXT and not a `data:` URL.
- No cover → SQL `NULL`, not an empty string `''`.
- Readers should tolerate legacy base64 TEXT covers (decode them) and treat `''` as no cover.

### 11.5 Asset lifecycle

- One `image_assets` row → many `images` placements.
- Deleting an asset: either delete all placements that reference it, or set their `asset_id = NULL`.

---

## 12. Full-text search

### 12.1 Create FTS5 table

```sql
CREATE VIRTUAL TABLE search_index USING fts5(
    item_id   UNINDEXED,
    type      UNINDEXED,
    title,
    url_path,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);
```

Set `db_meta.fts_enabled = '1'` and `fts_built = '1'` after populating.

### 12.2 What to index (one row per tree node)

| `content_tree.type` | `title` | `content` |
|---------------------|---------|-----------|
| Containers | entity `title` | `''` |
| `record` (HTML types) | `''` | plain text from HTML |
| `devanagari`, `bengali` | `''` | plain text from content |
| `wfw-pairs` | `''` | concat all `word` + `translation` plain text |
| `table` | `''` | concat all cell plain text |
| `image` | `images.caption` | `caption` + `alt` |
| `audio`, `video`, `file-link` | JSON `title` | `title` + `url` + `caption` |

All rows: set `url_path` from `url_paths` table (or `''`).

### 12.3 Plain-text extraction

Strip HTML tags; decode `&nbsp;` → space; trim. For JSON types, extract string values without tags.

### 12.4 BM25 ranking weights

`title × 8`, `url_path × 4`, `content × 1`

### 12.5 Build procedure

```
1. CREATE search_index (§12.1)
2. INSERT container rows (title + url_path, content = '')
3. INSERT record rows (extract content per §12.2)
4. UPDATE db_meta SET fts_built = '1'
```

Converters **should** build the index for production-ready files. Files without it may still open but search will be empty until rebuilt.

### 12.6 Legacy `content_fts`

Older `vidya-builder` files may have a `content_fts` virtual table instead of `search_index`. When reading such files, search may use `content_fts` until `search_index` is built. New files should use `search_index` only.

---

## 13. Authors

```sql
INSERT INTO authors (id, name, bio) VALUES (1, 'A. C. Bhaktivedanta Swami Prabhupada', NULL);

INSERT INTO author_links (id, type, item_id, author_id, role)
VALUES (1, 'book', 1, 1, 'author');
```

Roles: `author`, `translator`, `editor`, etc.

---

## 14. Wikilinks

Stored inside HTML `records.content`:

```html
<a class="wikilink" data-target-type="path" data-target="bg/2/13" data-label="BG 2.13">BG 2.13</a>
<a class="wikilink" data-target-type="id" data-target="4711" data-label="Note">Note</a>
```

- `data-target-type="path"` → `data-target` must exist in `url_paths.url_path`.
- `data-target-type="id"` → `data-target` is target `item_id`.

Build `url_paths` **before** inserting wikilinks that reference paths.

---

## 15. Legacy / `vidya-builder` compatibility

When **reading** older `.vidya` files (e.g. from `vidya-builder`) for conversion or merge:

| Legacy feature | How to handle |
|----------------|---------------|
| `backbone_id` column on entity tables | Ignore; do not read or write |
| `db_meta` without `UNIQUE` on `key` | Use UPDATE-then-INSERT for meta keys |
| Missing `sections`, `footnote_containers`, `image_assets`, `images` | Treat as empty; create tables if extending file |
| Missing `idx_content_tree_slug_per_parent` | Check for duplicate sibling slugs before creating index |
| Missing `search_index` | Build per §12; legacy `content_fts` may exist instead |
| `content_type` with underscores (`verse_text`) | Normalize to hyphens (`verse-text`) |
| `records.content_type` = `file` | Rename to `file-link` (Vidya Editor migrates on open) |
| Missing `editor_next_item_id` | Bootstrap as `1 + MAX(id)` across entity tables |

**New files** you create should:
- Use format version `2` and full schema (§6).
- Use hyphenated `content_type` names.
- **Never** write `backbone_id`.

---

## 16. Converting HTML

1. Split on block elements: `p`, `h1`–`h6`, `ul`, `ol`, `blockquote`, `table`, `img`, `center`.
2. One block → one record (§3).
3. Infer type (first match):

| Priority | Condition | `content_type` |
|----------|-----------|----------------|
| 1 | CSS class = known type id | that id |
| 2 | `h1`–`h3` | `sub-chapter` |
| 3 | `h4`–`h6` | `small-heading` |
| 4 | `blockquote` | `quote` |
| 5 | `ul` | `bullet-list` |
| 6 | `ol` | `numbered-list` |
| 7 | `center` tag | `verse-text` if mostly italic, else `paragraph-centered` |
| 8 | `text-align: center` | same as 7 |
| 9 | `text-align: right` | `reference` |
| 10 | default | `paragraph` |

4. Keep `<strong>`, `<em>`, `<u>`, `<br>`. Convert `<b>`→`<strong>`, `<i>`→`<em>`.
5. **Strip `<p>` / `</p>` wrappers** from stored content — save inline HTML only (§3).
6. `<table>` → `table` JSON (§3.16). `<img>` → image block (§3.20).

---

## 17. Converting Markdown

| Markdown | Vidya |
|----------|-------|
| `# Title` | `book` or `chapter` |
| `## Title` | `chapter` or `section` |
| `### 2.13` | `verse` (slug `2-13`) |
| paragraph | `paragraph` |
| `> quote` | `quote` |
| `**bold**` paragraph | `translation` |
| `- item` | `bullet-list` |
| `1. item` | `numbered-list` |
| `![alt](path)` | `image` block (§3.20) |
| pipe table | `table` JSON (§3.16) |
| `[^1]` | `footnote-container` + `footnote` |
| `[text](https://…/file.pdf)` | `file-link` JSON |
| `[text](youtube…)` | `video` or `audio` JSON |
| `[[path]]` | wikilink (§14) |

Inline: `**`→`<strong>`, `*`→`<em>`, `__`→`<u>`. Use `<br>` for line breaks inside verse-type records. Do not wrap in `<p>`.

---

## 18. Media URL detection

```
# YouTube
youtu.be/ID → provider=youtube, embedId=ID
youtube.com/watch?v=ID → provider=youtube, embedId=ID
youtube.com/embed/ID → provider=youtube, embedId=ID

# Vimeo
vimeo.com/123 → provider=vimeo, embedId=123
player.vimeo.com/video/123 → provider=vimeo, embedId=123

# SoundCloud
soundcloud.com/… → provider=soundcloud

# Direct files
Audio: .mp3 .m4a .aac .ogg .wav .flac .opus → provider=file
Video: .mp4 .webm .ogv .mov .m4v → provider=file

# Fallback
provider=generic
```

Prepend `https://` if URL lacks scheme.

---

## 19. Editing existing files

### Update record

```sql
UPDATE records SET content = ?, content_type = ? WHERE id = ?;
UPDATE content_tree SET type = ? WHERE item_id = ?;  -- if structured type changed
```

### Move node

```sql
UPDATE content_tree SET parent_id = ?, sort_order = ? WHERE item_id = ?;
UPDATE <hierarchy_table> SET parent_id = ? WHERE id = ?;
-- rebuild url_paths for subtree
```

### Delete subtree

Delete `url_paths`, entity rows, `content_tree` rows for node + all descendants. Do not reuse ids.

### Replace all blocks under a container

1. List existing record/image child ids.
2. Update existing or insert new (allocate ids).
3. Delete removed children.
4. Set `sort_order` = 0, 1, 2, …

---

## 20. Validation checklist

- [ ] Full schema §6 with all indexes
- [ ] `vidya_format_version = 2`
- [ ] `editor_next_item_id` > max used id
- [ ] Every `content_tree.item_id` has matching entity row
- [ ] No orphan `parent_id`
- [ ] Entity `parent_id` mirrors `content_tree.parent_id` for containers
- [ ] `is_page`: 1 containers, 0 records/images
- [ ] Structured types: `content_tree.type` = `records.content_type`
- [ ] Generic HTML/media: `content_tree.type = 'record'`
- [ ] Images in `images`, not `records`
- [ ] Slugs valid; empty → NULL; unique per sibling
- [ ] `url_paths` consistent with slugs
- [ ] All 19 content types valid per §3
- [ ] `search_index` built if search required (§12)
- [ ] Hyphenated `content_type` names only

### Diagnostic SQL

```sql
-- Orphan parents
SELECT item_id FROM content_tree
WHERE parent_id IS NOT NULL
  AND parent_id NOT IN (SELECT item_id FROM content_tree);

-- Records without entity
SELECT item_id FROM content_tree
WHERE type IN ('record','devanagari','bengali','wfw-pairs','table')
  AND item_id NOT IN (SELECT id FROM records);

-- Images without entity
SELECT item_id FROM content_tree WHERE type = 'image'
  AND item_id NOT IN (SELECT id FROM images);

-- Duplicate sibling slugs
SELECT parent_id, slug, COUNT(*) FROM content_tree
WHERE slug IS NOT NULL GROUP BY parent_id, slug HAVING COUNT(*) > 1;
```

---

## 21. Worked examples

### 21.1 Complete verse document

```sql
-- Book id=1, Chapter id=2, Verse id=3
-- url_paths: bg/2/13

INSERT INTO books (id, title, type, parent_id) VALUES (1, 'Bhagavad-gita', 'book', NULL);
INSERT INTO content_tree VALUES (NULL, NULL, 'book', 1, 'bg', 1, 0);

INSERT INTO chapters (id, title, parent_id) VALUES (2, 'Chapter 2', 1);
INSERT INTO content_tree VALUES (NULL, 1, 'chapter', 2, '2', 1, 0);

INSERT INTO verses (id, title, parent_id) VALUES (3, '2.13', 2);
INSERT INTO content_tree VALUES (NULL, 2, 'verse', 3, '13', 1, 0);

INSERT INTO records VALUES (10, 'devanagari', 'धृतराष्ट्र उवाच');
INSERT INTO content_tree VALUES (NULL, 3, 'devanagari', 10, NULL, 0, 0);

INSERT INTO records VALUES (11, 'translation', '<strong>Dhritarastra said:</strong>');
INSERT INTO content_tree VALUES (NULL, 3, 'record', 11, NULL, 0, 1);

INSERT INTO records VALUES (12, 'wfw-pairs', '[{"word":"dharma-kṣetre","translation":"in the place of pilgrimage"}]');
INSERT INTO content_tree VALUES (NULL, 3, 'wfw-pairs', 12, NULL, 0, 2);

INSERT INTO records VALUES (13, 'table', '{"columns":[{"id":"c1"},{"id":"c2"}],"rows":[{"headerRow":true,"cells":{"c1":{"content":"Word"},"c2":{"content":"Meaning"}}},{"cells":{"c1":{"content":"dharma"},"c2":{"content":"duty"}}}]}');
INSERT INTO content_tree VALUES (NULL, 3, 'table', 13, NULL, 0, 3);

INSERT INTO image_assets (id, data, mime, width, height) VALUES (20, X'...', 'image/webp', 400, 300);
INSERT INTO images (id, asset_id, alt, size, align) VALUES (15, 20, 'Soul diagram', 'full', 'center');
INSERT INTO content_tree VALUES (NULL, 3, 'image', 15, NULL, 0, 4);

INSERT INTO url_paths (url_path, type, item_id) VALUES ('bg', 'book', 1);
INSERT INTO url_paths (url_path, type, item_id) VALUES ('bg/2', 'chapter', 2);
INSERT INTO url_paths (url_path, type, item_id) VALUES ('bg/2/13', 'verse', 3);
```

---

## 22. Common mistakes

| Mistake | Fix |
|---------|-----|
| `content_tree.type = 'paragraph'` | `type='record'`, `content_type='paragraph'` |
| Image in `records` | `images` + `image_assets` + `type='image'` |
| `wfw-pairs` with `type='record'` | `type='wfw-pairs'` |
| `table` with `type='record'` | `type='table'` |
| Table JSON as array | Must be object with `columns` + `rows` |
| WFW JSON as object | Must be array of pairs |
| Empty slug `''` | SQL `NULL` |
| Leading `/` in url_path | `bg/2/13` not `/bg/2/13` |
| `verse_text` | `verse-text` |
| Cover in `image_assets` | `books.cover` only |
| Cover as base64 TEXT or `''` | WebP BLOB, or `NULL` when absent |
| Skipping `url_paths` | Always build §9 |
| Skipping `search_index` | Build §12 for searchable libraries |

---

## 23. Agent task checklist

1. Inventory source content — all types, images, tables, footnotes, media.
2. Map every element to §2 / §3 / §4 — **do not drop content**.
3. Create schema §6; write `db_meta` §7.
4. Allocate ids §8.
5. Insert containers §4 with slugs §10.
6. Insert every record type §3 with correct `sort_order`.
7. Process images §3.20 / §11; deduplicate assets.
8. Build `url_paths` §9.
9. Build `search_index` §12.
10. Run validation §20.

**All content types to support:**

`paragraph`, `paragraph-centered`, `verse-text`, `translation`, `reference`, `sub-chapter`, `small-heading`, `footnote`, `bullet-list`, `numbered-list`, `quote`, `audio`, `video`, `file-link`, `devanagari`, `bengali`, `wfw-text`, `wfw-pairs`, `table`, `image`

**All container types:**

`book`, `canto`, `chapter`, `verse`, `section`, `footnote-container`, `transcript`, `letter`

This document is the **complete specification** for the `.vidya` file format.

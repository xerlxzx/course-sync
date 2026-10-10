# Developer Guide

This guide covers the internals of `course_sync.py`, and how it fits with its two companion scripts, for contributors and developers who want to understand, extend, or debug the tool (part of the `course-sync` project). It assumes Python familiarity but not Moodle-specific knowledge.

See also: [Moodle API Reference](MOODLE_API.md) for endpoint and field details.

## Contents

1. [Architecture overview](#1-architecture-overview)
2. [Moodle web services primer](#2-moodle-web-services-primer)
3. [Module type handlers](#3-module-type-handlers)
4. [Lesson-folder detection heuristic](#4-lesson-folder-detection-heuristic)
4b. [Noise routing: `_misc/` and skip patterns](#4b-noise-routing-_misc-and-skip-patterns)
4c. [Rich-text capture: `_content.md`](#4c-rich-text-capture-_contentmd)
5. [Dedup system](#5-dedup-system)
6. [URL conversion and the double-webservice bug](#6-url-conversion-and-the-double-webservice-bug)
6b. [Token safety and error handling](#6b-token-safety-and-error-handling)
7. [Filename sanitization](#7-filename-sanitization)
7b. [Filename collisions](#7b-filename-collisions)
8. [Audit files: _index.md and _links.md](#8-audit-files-_indexmd-and-_linksmd)
9. [Adding a new module handler](#9-adding-a-new-module-handler)
10. [Out of scope (intentional)](#10-out-of-scope-intentional)
11. [Testing protocol](#11-testing-protocol)
11b. [Tests](#tests)
12. [Contributing](#12-contributing)

---

## 1. Architecture overview

The Moodle downloader lives in a single file: `course_sync.py`. This is a deliberate choice: the tool has a narrow scope, and a single file is easier for students and casual contributors to read, copy, or fork than a package with multiple modules. No packaging, no `setup.py`, no entry points. Two small companion scripts sit beside it; see [Companion tools](#companion-tools-and-the-contract-between-them).

**Pipeline:**

```
config load
    -> token resolution (--token > token_file > MOODLE_TOKEN)
    -> API auth (core_webservice_get_site_info)
    -> enumerate enrolled courses (core_enrol_get_users_courses)
    -> prefix-match enrolled courses against config codes
    -> for each matched course:
        load hash index
        -> get course sections (core_course_get_contents)
        -> for each section:
            -> section_dir_for(): Assessments/ | WeekN/ | name-derived fallback folder
            -> for each module in section (a failure is logged as [fail] and the loop goes on):
                -> dispatch_module() routes by modname
                -> file handlers call _place_file(): skip rules, _misc/ and LessonN/
                   routing, claim_name(), then download_file()
                -> download_file() applies tiered dedup, writes bytes
            -> write _links.md (if external links found; remove a stale generated one if none)
            -> write _content.md (if prose was captured)
            -> write _index.md (if any rows/links/skipped)
        save hash index (per course, not per run; also after a failure)
    -> exit 1 if anything failed, else 0
```

**Key source regions:**

| Content | Key symbols |
|---|---|
Regions appear in the file in this order, each under a `# ----` banner:

| Region | Key symbols |
|---|---|
| Secret redaction | `register_secret`, `strip_secrets`, `redact`, `describe_error`, `API_TIMEOUT` |
| Moodle web service API | `api`, `get_user_id`, `parse_moodle_version`, `get_courses`, `get_contents` |
| Section routing and name cleaning | `section_to_week_folder`, `section_folder_name`, `section_dir_for`, `lesson_subfolder_from_filename`, `is_assessment_section`, `sanitize_filename` (with `_shorten_name`, `MAX_NAME_BYTES`), `collapse_ws`, `clean_section_name`, `short_course_code` |
| Download URLs and display paths | `is_moodle_file_url` (the token gate), `make_download_url` (host assertion, double-webservice guard), `fetch_moodle_file` (same-site redirects only), `claim_name` / `file_source_key` (filename collisions), `_rel_for_display` |
| HTML-to-Markdown | `html_to_markdown`, `extract_heading_from_html`, `is_content_block_skippable` |
| Noise routing | `DEFAULT_MISC_PATTERNS`, `MISC_LABEL_NAME_SUBSTRINGS`, `is_misc_file`, `should_skip_file`, `label_is_misc`, `_classify_misc_reason` |
| Hash index | `load_hash_index` (quarantines a corrupt file), `save_hash_index`, `forget_hash_path`, `record_hash_path`, `record_dup`, `forget_dup`, `known_dup_original`, `_live_holder` |
| File download | `download_file`: tiered dedup, `_remote_looks_newer`, `_write_bytes` |
| HTML link extraction | `extract_links_from_html`, `_strip_tags` |
| Module handlers | `_row`, `_dest_label`, `_place_file`, `record_failure`, `_write_text_if_changed`, `save_page_markdown`, `handle_book_module`, `handle_contents_module`, `handle_label_module`, `handle_url_module`, `handle_description_module`, `handle_unsupported_module` |
| Unit guide | `_is_unit_guide_url`, `select_unit_offering`, `pdf_fingerprint`, `handle_unit_guide` |
| Section output | `write_links_file` (and `_remove_stale_links_file`), `write_section_content`, `write_section_index` |
| Orchestration | `new_section_state`, `dispatch_module`, `sync_course` |
| Config and token loading | `load_config`, `resolve_path`, `load_token`, `parse_pattern_list`, `parse_courses` |
| Interactive setup wizard (`--setup`) | `run_setup` |
| CLI entry point | `run_guarded`, `main` (a thin wrapper), `_main` |

### Companion tools and the contract between them

```
workspace launcher (outside the repo)  ->  sync_all.sh (shim)  ->  scripts/sync_all.sh  ->  src/course_sync.py   (.venv)
                                                                                         ->  src/sway_sync.py     (.venv-sway)
src/sync_one.py  ->  imports course_sync.py from its own directory (runs in course_sync's .venv)
```

- **`scripts/sync_all.sh [--dry-run]`** `cd`s to the repo root, runs `src/course_sync.py` and then `src/sway_sync.py`, and exits non-zero if either failed. The order is load-bearing (see the next point). With `--dry-run` it runs `course_sync.py --dry-run` and then `sway_sync.py --check` (preview only; `--check`'s exit 3, "changes found", is printed as a NOTE and is not a failure). Any other argument prints the usage and exits 2 before anything runs. A root `sync_all.sh` shim execs it (passing `"$@"` through), because the workspace launcher `cd`s into the repo and execs `./sync_all.sh`; keep the shim unless you also change that launcher.
- **`_links.md` is the only data passed between the tools.** `write_links_file` writes `- [title](url)` lines; `sway_sync.discover_decks` greps them for `sway.cloud.microsoft` URLs. If you change that line format, change `SWAY_LINK_RE` in `sway_sync.py` too.
- **`config.yaml` is shared.** `sway_sync.py` takes its unit folders from `courses[].folder` and its output root from `output_dir`, with the same meaning as in `course_sync.py`. It cannot use PyYAML (its venv does not have it), so `read_layout` handles only the shapes the config uses and falls back to a built-in layout on anything else. If you add config syntax, check `read_layout` still refuses or understands it.
- **`sway_sync.py` must not import `course_sync`.** `course_sync` imports `requests`, `PyYAML` and `markdownify` at module level; `.venv-sway` has none of them. `sync_one.py` can import it because it runs in `course_sync`'s venv.
- **Code is in `src/`, but the repo root is the runtime root.** Both tools define `REPO_DIR = Path(__file__).resolve().parents[1]` and keep their files there: `config.yaml`, `.course_sync_token`, `.sway-auth.json`, `.sway-profile/`, `.sway-state.json`, and the `.venv` / `.venv-sway` interpreters (`sway_sync.py` re-execs itself under `REPO_DIR/.venv-sway/bin/python`). `DEFAULT_CONFIG_PATH` is `REPO_DIR/config.yaml`, so `output_dir: ..` still means the folder above the repo. The Moodle dedup index lives under `output_dir` instead (`.course_sync/downloaded.json`). All of these are gitignored. `src/` is not called `course_sync/` on purpose: that would shadow `import course_sync` in `sync_one.py`.
- **`src/sync_one.py <cmid> [--dry-run] [--config PATH]`** (puts `src/` on `sys.path` itself, so it works from any cwd) re-fetches one module through `dispatch_module`, the same router `sync_course` uses, with `section_dir_for` for the destination, so a book, page, resource, folder, url or label lands exactly where a full sync would put it. It never rewrites `_index.md`, `_links.md` or `_content.md`, so a module whose only output lives there (quiz or assign description, label text, an external link, an unsupported type, a locked book) writes nothing; it says so and exits 3. Exit status: 0 handled; 1 error, failed fetch or cmid not found; 2 bad command line; 3 nothing to write. Before handling the module it replays the modules ahead of it as a silent dry run (`prime_name_claims`), so a clashing filename gets the same `(2)` name as in a full sync (see [section 7b](#7b-filename-collisions)).

---

## 2. Moodle web services primer

All API calls go to:

```
GET {base_url}/webservice/rest/server.php
    ?wstoken=TOKEN
    &wsfunction=FUNCTION_NAME
    &moodlewsrestformat=json
    &...params
```

The `api()` helper handles all of this; callers just pass the function name and keyword params.

**`core_webservice_get_site_info`**: Returns metadata about the site and the authenticated user. The tool reads `userid` (to call `get_courses`) and `fullname` (to display on startup). No additional parameters needed.

**`core_enrol_get_users_courses`**: Takes `userid`. Returns a list of course objects for all courses the user is enrolled in. The tool reads `id` (course ID for content fetching) and `shortname` (for prefix matching against config codes).

**`core_course_get_contents`**: Takes `courseid`. Returns a list of section objects. Each section has a `name` string and a `modules` list. Each module has:

- `modname`: the module type string (e.g. `resource`, `folder`, `page`, `label`, `url`, `forum`, `quiz`, `assign`, etc.)
- `name`: the display name set by the lecturer
- `description`: HTML string (present on `label` modules; sometimes present on others)
- `contents`: list of file/content objects (present on `resource`, `folder`, `page`, `url` modules)

Each object in `contents` has:

- `type`: `"file"` for downloadable files, `"url"` for redirects
- `filename`: original filename (may be URL-encoded)
- `fileurl`: the Moodle pluginfile URL (requires token to download)

**Moodle API errors** come back as JSON with an `"exception"` key instead of raising an HTTP error code. The `api()` helper detects this and raises `RuntimeError`.

---

## 3. Module type handlers

The dispatch logic is `dispatch_module`, shared by `sync_course` and `sync_one.py`. Each branch returns the number of files it downloaded:

```python
if modname in ("resource", "folder", "page"):
    return handle_contents_module(...)
if modname == "label":
    return handle_label_module(...)
if modname == "url":
    return handle_url_module(...)
if modname == "book":
    return handle_book_module(...)
if modname in ("quiz", "assign"):
    return handle_description_module(mod, section_state)
return handle_unsupported_module(mod, section_state)
```

`sync_course` calls it inside a `try` per module: an exception from a handler is logged as `[fail] <module> (<modname>): <reason>`, recorded under `Skipped` and counted, and the loop moves on (see [section 6b](#6b-token-safety-and-error-handling)). Every handler that fetches a file takes `base_url` and `token` as its first two arguments, like the API functions.

### `handle_contents_module` (resource / folder / page)

Iterates `mod["contents"]`, filtering to entries with `type == "file"`. For each file:

1. Reads `fileurl` and `filename` from the content entry.
2. Sanitizes the filename.
3. For a `page` module, `index.html` IS the page content, so it goes to `save_page_markdown` (below) instead of being downloaded.
4. Everything else goes to `_place_file()`, which applies skip rules, `_misc/` and lesson-subfolder routing, calls `download_file()` and appends the `_index.md` row (see below).

### `_place_file`

The shared tail of every handler that downloads a file (`handle_contents_module`, `handle_label_module`, `handle_url_module`). In order: `should_skip_file` (a hard skip, recorded under `Skipped`), `is_misc_file` or a forced `misc_reason` (label modules flagged by `label_is_misc`), the `LessonN/` heuristic, `claim_name()` (a different file whose name matches one already placed in that folder, ignoring case, becomes `name (2).ext`; see [section 7b](#7b-filename-collisions)), `download_file()` (passing the server's `filesize` / `timemodified` when the API provided them, so an in-place replacement on the server is detected), then the `[misc]` log line and the `section_state` rows. The `download_file()` call is wrapped in a `try`: a 404, timeout or `OSError` becomes `[fail] <path>: <reason>` via `record_failure()` and the function returns `None`, so one bad file cannot stop the module's other files. It returns `download_file`'s outcome (`"downloaded"`, `"skipped"`, `"dup"`, `"dry"`) or `None` when nothing was fetched. A new file-downloading handler should call it rather than repeat the sequence.

### `save_page_markdown` (page)

Fetches the page's `index.html`, converts it with `html_to_markdown()` and writes `<page name>.md` in the section folder (not into a `LessonN/` subfolder), prefixed with a short header noting that it was captured by course-sync. Output carries no timestamps, so an unchanged page is byte-identical and `_write_text_if_changed` skips the write (`[ok] ... (page unchanged)`). Files attached to the page are still downloaded by the normal path. The file name goes through `claim_name`, so two pages with the same name become `Name.md` and `Name (2).md`. A failed fetch goes through `record_failure()` (`[fail]` line, `Skipped` entry, non-zero exit).

### `handle_book_module` (book)

A book's `contents` holds one `index.html` per chapter (filepath `/1/`, `/2/`, ...) plus a `structure` entry whose JSON `content` carries the chapter titles. The handler sorts chapters by number, fetches each, and writes one `<book name>.md` with a `##` heading per chapter. A time-locked book (`uservisible` false, no contents) is reported as `[locked]` with Moodle's availability text and recorded under `Skipped`, not treated as an error; the next sync after the release date captures it. A book with no chapters and no lock prints a `[warn]`. Neither claims a file name. Output is deterministic, as for pages, and the name goes through `claim_name`. A chapter that fails to fetch is recorded with `record_failure()` and shown in the file as `*(chapter fetch failed: ...)*`; if the book file already exists the handler logs `[keep]` and leaves the earlier, complete copy alone instead of replacing it with one that has a hole.

### `handle_label_module` (label)

Label modules store content as HTML in `mod["description"]`. Calls `extract_links_from_html()` to find all `<a href>` and `<img src>` URLs in that HTML.

For each link found:

- If `is_moodle_file_url(link, base_url)` is true (same scheme, host and port as `moodle.base_url`, and `/pluginfile.php/` in the path): treat as a downloadable file, extract the filename from the URL path and pass it to `_place_file()`. Files from a label that `label_is_misc` flags are forced into `_misc/`.
- Any other URL that starts with `"http"`, including a `pluginfile.php` link on a different host (another university's Moodle, say): append to `section_state["external_links"]`. It is never fetched and never sent the token.

This is the mechanism that catches the common Moodle pattern of embedding slide download links inside a text label.

### `handle_url_module` (url)

URL modules store their target in `mod["contents"][0]["fileurl"]`. If `is_moodle_file_url` accepts it, it is downloaded like a file. Otherwise, the module's `name` is used as the display text and the URL is recorded as an external link.

### `handle_description_module` (quiz / assign)

These modules have no downloadable files via the content API. The handler reads `mod["name"]` and `mod["description"]`. The description HTML is converted to Markdown with `html_to_markdown()`; if the description is empty, a stub line (`*Quiz: <name>*` or `*Assign: <name>*`) is used. The result is appended to `section_state["content_blocks"]` under the module name as heading, then written to `_content.md` with the rest of the section's prose.

### `handle_unsupported_module` (everything else)

Any `modname` without a handler (e.g. `forum`, `glossary`, `attendance`, `h5pactivity`) lands here. It is never a silent drop: the handler prints `[warn] unsupported module type '<modname>': <name> - NOT captured, check it on iLearn` and appends an `UNSUPPORTED module type` entry to `section_state["skipped"]`, which `write_section_index` renders under `## Skipped`. If the module has a description that passes `is_content_block_skippable`, it is also kept as a `_content.md` block labelled `<modname> (unsupported)`, so the visible text is not lost. To support a new type, give it its own branch in `sync_course` (see [section 9](#9-adding-a-new-module-handler)) and it stops reaching this catch-all.

### `extract_links_from_html`

Extracts `(url, display_text)` pairs from an HTML string. Processes anchor tags first, recording their span positions. Then processes `<img src>` tags, skipping any that fall inside an already-processed anchor span (to avoid double-counting images that are also links).

Excludes fragment-only URLs (`#...`) and `mailto:` links.

---

## 3b. Unit guide PDF download

Unit guide links appear as `url` modules in sections that the main loop would otherwise process normally (typically the "General" section). However, they are also pre-scanned in a separate pass before the main section loop so they are captured even in sections the main loop might otherwise skip.

**`_is_unit_guide_url(url: str) -> bool`**: Checks whether the URL's hostname contains `"unitguide"`. This heuristic catches portals such as `unitguides.example.edu`.

**`select_unit_offering(source_url, response_url, body, period=None) -> Optional[str]`**: Picks the offering ID. A `/unit_offerings/<ID>` already in the redirect URL wins. Otherwise it scans the search page's offering links, keeping those whose text contains the course code and, when `unit_guide_period` is configured, matches the teaching period in the module's `full_code` (the `from_shortname` regex captures the number, which is substituted for `{n}` in the `label` regex), and returns an ID only when exactly one distinct offering remains; ambiguity returns `None` rather than guessing a term.

**`handle_unit_guide(fileurl, course_dir, dry_run, output_dir, hash_index, downloaded_urls, period=None) -> bool`**: Fetches the unit guide portal page, calls `select_unit_offering` on the redirect URL and response body, then constructs and downloads the printer-friendly PDF from `.../unit_offerings/<ID>/unit_guide/print.pdf`. The PDF is saved as `Unit_Guide.pdf` in the course root (not inside a weekly section folder). Returns `True` if a file was actually written.

**Fingerprint comparison.** The portal's renderer writes a new random trailer `/ID [<hex><hex>]` (and fresh info dates) on every render, so two fetches of an unchanged guide never match byte for byte. `pdf_fingerprint(body)` is the SHA-256 of the bytes with the `/ID` array, `/CreationDate` and `/ModDate` masked out. If the fetched and the on-disk fingerprints are equal the file is left alone and `[skip] ... (unchanged)` is logged; otherwise it is replaced (`[dl]` for a new file, `[upd]` for a changed one). Skipping when the offering id is unchanged would be wrong, because the convenor can edit a guide without changing its id. On a skip the hash index records the hash of the bytes on disk, not the fresh render's.

Failure modes (each prints a `[warn]` line and returns `False`):
- Network error fetching the portal page
- No single offering could be matched (`select_unit_offering` returned `None`)
- Network error downloading the PDF
- Response `Content-Type` does not contain `"pdf"`

The unit guide is not subject to tier-2 content dedup; only the fingerprint comparison above decides whether it is rewritten.

---

## 3c. Section folder routing

`section_dir_for(course_dir, sec_name, section_index)` is the single place that decides a section's folder; `sync_course` and `sync_one.py` both call it, so they cannot drift apart. `sec_name` is the cleaned name (`clean_section_name`). Precedence:

1. `is_assessment_section(name)` routes to `Assessments/`. This wins over week names, so "Week 3 Quiz" is an assessment, not `Week3/`.
2. `section_to_week_folder(name)` handles the `"Week N"` pattern (`Week3`, or `Week5_StudyWeek` when the name also contains "study").
3. All remaining sections fall through to `section_folder_name(sec_name, section_index)`.

The folder's own name doubles as the section label in `_index.md` and `_links.md` (`sync_course` uses `section_dir.name`).

**`section_folder_name(sec_name: str, section_index: int) -> str`**: Strips HTML tags from the section name (Moodle occasionally wraps section names in `<b>` or similar), sanitizes the result, and replaces spaces with underscores. The section index is used as a fallback: index 0 without a usable name becomes `_General`; other indices become `_Section<N>`. This ensures every section with content gets a folder, not just weekly ones.

---

## 4. Lesson-folder detection heuristic

```python
def lesson_subfolder_from_filename(filename: str) -> Optional[str]:
    m = re.search(r"[Ll]esson\s*(\d+)", filename)
    if m:
        return f"Lesson{m.group(1)}"
    return None
```

The heuristic looks only at the **filename**, not the module name or section name. A file is placed in `LessonN/` if its name contains `"Lesson N"` or `"lesson N"` (case-insensitive, optional whitespace between "Lesson" and the number).

**Known limitation:** The section folder is determined by the Moodle section the module lives in; the lesson subfolder is determined by the filename alone. These are independent. A file named `Week3_Lesson2_diagram.png` in the Week 1 section ends up at `Week1/Lesson2/Week3_Lesson2_diagram.png`. The Week 1 directory is from Moodle's section, and the Lesson2 subfolder is from the filename. There is no cross-check.

This behaviour is predictable and documented here. Changing it would require either trusting the module name (inconsistent across institutions) or doing a second pass after full enumeration (more complex, currently out of scope).

---

## 4b. Noise routing: `_misc/` and skip patterns

Some files are part of the course but aren't lecture material: the university logo embedded in every page, the land photo inside "Acknowledgement of Country" labels, assignment templates, etc. The tool routes these to a `_misc/` subfolder inside each section, and also supports a hard-skip list for files that shouldn't even be downloaded.

### Three decision inputs

1. **`DEFAULT_MISC_PATTERNS`**: module-level constant. Regex patterns matched against the basename (post-sanitization). Catches logos, generic poster/academic/presentation/essay/report templates, and bare `template.{pptx,docx,xlsx}` files. Always active; no config required.
2. **`misc_patterns` from `config.yaml`**: optional list of regex strings. Extends (does not replace) the defaults. Use to extend per-institution conventions without modifying the script.
3. **`MISC_LABEL_NAME_SUBSTRINGS`**: module-level constant. Case-insensitive substrings of a label module's `name` field. If any of these appear in a label's name, every file extracted from that label is routed to `_misc/`. Currently `["acknowledgement"]`.

A separate `skip_patterns` config field controls *hard* skips: files that match are never downloaded and never written, only recorded in `_index.md` under `Skipped` with reason `matched skip_patterns`.

### Helper functions

```python
def is_misc_file(filename: str, extra_patterns: List[str]) -> Optional[str]:
    """Returns a short reason if filename matches misc patterns, else None."""

def should_skip_file(filename: str, skip_patterns: List[str]) -> bool:
    """Returns True if filename matches any skip pattern."""

def label_is_misc(mod_name: str) -> bool:
    """Returns True if a label's name marks all its content as misc."""
```

Invalid regex patterns from config are caught (`re.error`) and ignored silently rather than crashing the sync.

### Threading through handlers

`main` parses `misc_patterns` / `skip_patterns` once via `parse_pattern_list`, then `sync_course` forwards both lists to each file handler. The handlers pass them to `_place_file`, which checks `should_skip_file` first; if false, it computes a `misc_reason` (either via `is_misc_file` or, for labels, the forced reason derived from `label_is_misc`). When a reason is set, the destination becomes `section_dir / MISC_SUBFOLDER`, overriding lesson-folder detection. `_place_file` logs `[misc]` and appends to `section_state["misc_rows"]` so `write_section_index` can render the `Routed to _misc/` table.

### Precedence (in order)

A `page` module's `index.html` is not a candidate file: it is rendered to Markdown by `save_page_markdown` before any of these rules apply. For every other candidate file, decisions run in this order:

1. **`[skip-rule]`**: basename matches `skip_patterns`. File is not downloaded. Recorded in `section_state["skipped"]`.
2. **`_misc/` routing**: basename matches a default `misc_patterns` entry, a user `misc_patterns` entry, OR (for labels) `label_is_misc(mod_name)` is true. Destination overrides the lesson-folder destination. Default patterns are checked before user patterns so the reason string in `_index.md` stays specific (`logo pattern` / `template pattern`) rather than the generic `matched misc_patterns`.
3. **Lesson-folder detection**: existing `lesson_subfolder_from_filename` heuristic.
4. **Section root**: fallthrough.

`_misc/` takes precedence over lesson detection: a logo file named `Lesson 2 Logo.png` lands in `_misc/`, not `Lesson2/`.

### Interaction with hash dedup

`_misc/` routing is decided *before* `download_file` is called, so the destination path is final by the time the tiered dedup runs. On a fresh install, a misc file is fetched, hashed, and written into `_misc/` like any other file.

On an *upgrade* run (existing user, files already on disk under the pre-`_misc/` routing), the same file shows up as a `[dup]` against its old path: the bytes are fetched, hashed, found in `hash_index["files"]`, and recorded as a duplicate (see tier 1c in section 5) but the file is not re-written. The `Routed to _misc/` table in `_index.md` still records what *would* land there, giving the user a clear migration audit. The user can then manually move the old file (or delete it and let a fresh sync rebuild); the dedup index will handle either gracefully.

This is intentional and matches the documented behaviour for any routing change: hash-dedup wins to avoid double-storing bytes; `_index.md` makes the disagreement visible.

---

## 4c. Rich-text capture: `_content.md`

Label descriptions, section summaries, and similar HTML fragments inside Moodle hold a non-trivial amount of prose that is *not* a file: assessment briefs, academic integrity statements, lesson framing, etc. Mining these for `<a href>` and `<img src>` was already in scope (see `handle_label_module`), but the readable prose itself was being discarded. The `_content.md` capture path preserves that prose as Markdown.

### Conversion

`html_to_markdown(html_str: str) -> str` runs the HTML through the third-party [`markdownify`](https://pypi.org/project/markdownify/) library with ATX headings and `-` bullets. Before conversion it strips the Moodle-added `<div class="no-overflow">` wrapper; after conversion it collapses 3+ blank lines to 2.

`markdownify` was chosen over rolling our own converter because real Moodle content contains HTML tables (assessment summaries are often multi-column tables) and writing a robust table walker is more work than the rest of the feature combined. It is the first new dependency since `requests` and `PyYAML`; pure-Python; ~30 KB.

### Heading heuristic

`extract_heading_from_html(html_str, fallback_name)` returns the text of the first `<h2>`/`<h3>`/`<h4>` it finds in the description. The fallback chain handles the Moodle quirk where label `name` fields are auto-generated as the first ~100 characters of the description in UPPERCASE (e.g. `"THE TABLE BELOW PROVIDES A SUMMARY OF THE ASSESSME..."`), while the description itself usually has a clean `<h3>` or `<h4>` inside. Falling back to a 60-char truncation of the label name handles labels whose body has no heading at all.

### Skip rules

`is_content_block_skippable(markdown_body)` returns True for content the user does not want in `_content.md`:

1. Empty / whitespace-only after conversion.
2. Body collapses to nothing after stripping every `[text](url)` link (a label whose only content is a navigation anchor).
3. Body is under 30 characters AND either contains "back to top" OR is just a single heading line.

Misc labels (those whose `name` matches `MISC_LABEL_NAME_SUBSTRINGS`, e.g. "Acknowledgement of Country") are always skipped from `_content.md`. The same substring check that already routes their files to `_misc/` also suppresses their prose.

### Section summaries

Section summaries (`section["summary"]`) have no module-level `name` to apply `label_is_misc` against, so `sync_course` checks the stripped summary HTML text for the same `MISC_LABEL_NAME_SUBSTRINGS` values before conversion. This suppresses Acknowledgement of Country boilerplate embedded directly in a section summary, matching the label-level filtering above.

Non-misc summaries are captured if non-empty and not skippable. They are appended before module-derived label blocks so `_content.md` preserves the section-level framing first.

### Threading

`new_section_state()` gives every section an empty `content_blocks: List[Tuple[str, str, str]]` (alongside `rows`, `misc_rows`, `external_links` and `skipped`) before `sync_course` iterates modules. The section-summary capture runs first, skips acknowledgement-like boilerplate summaries, and appends a `("(heading)", "section summary", md)` entry when the remaining Markdown passes `is_content_block_skippable`. `handle_label_module` calls `is_content_block_skippable` and `extract_heading_from_html` after the existing link-extraction pass and appends `("(heading)", "label", md)` if the body passes. `write_section_content` reads the list, writes `_content.md` if non-empty, and returns whether it did so. The boolean is forwarded into `write_section_index` so the "Prose content" cross-reference renders.

### Names and entities

`clean_section_name(name)` runs `html.unescape` so `Week 1: Intro &amp; Hygiene` becomes `Week 1: Intro & Hygiene`. The decoded form is used everywhere the section name is shown: `_content.md` heading, `_index.md` heading, the per-section log line.

`short_course_code(shortname)` keeps the leading `[A-Za-z]+\d+` run of a Moodle shortname (e.g. `UNIT1000_T2_2026_ALL` → `UNIT1000`). Used only for the `# Section - CODE` heading in `_content.md`. Falls through to returning the raw shortname if no letters-followed-by-digits prefix is found.

---

## 5. Dedup system

**Tier 1a (per-run URL set):** `downloaded_urls: Set[str]` is initialised per course and passed through to every `download_file()` call. If the same `fileurl` appears in multiple modules (a common occurrence when lecturers link the same file from multiple places), only the first encounter is processed.

**Tier 1b (path on disk):** If the output path already exists as a file, it is printed as `[skip]` and not re-fetched unless the server metadata says it changed (`_remote_looks_newer`: a different `filesize`, or a `timemodified` newer than the local mtime). This is the primary mechanism for fast re-runs. When the metadata changed but the fetched bytes are identical to the local file (a re-upload of the same content), the log says `metadata changed, content identical` and the local mtime is set to the server's `timemodified` (`os.utime`), so the next run's mtime check passes without another GET. Names are decided before this tier by `claim_name` (see [section 7b](#7b-filename-collisions)), so tier 1b only ever compares a path with the one file that owns that name.

**Tier 1c (recorded duplicates):** A content duplicate is never written at its own path, so tier 1b cannot see it. When tier 2 finds a duplicate it stores a record in the index under `"dups"` (keyed by the would-be path): the content hash and size, the path of the original, and the `filesize` / `timemodified` the server reported. On later runs `known_dup_original()` returns the original and `download_file` prints `[dup] ... recorded, not re-fetched` without any GET (also under `--dry-run`), while (a) the server metadata equals what was recorded and (b) a copy of the content still verifies on disk. Links scraped from label HTML have no metadata, on either side, which counts as equal: the record is trusted, as tier 1b trusts a file it cannot compare. The record is dropped, and the file fetched normally, when the metadata changes, when the original is gone or has been replaced by other content, or when the path gets a real file.

**Tier 2 (SHA-256 hash index):** For files that pass the earlier checks, the full file content is fetched into memory. A SHA-256 hash of the bytes is computed. If that hash is in `hash_index["files"]` and `_live_holder()` confirms that a recorded path really holds those bytes, the file is a duplicate, logged as `[dup]` with that path, recorded as in tier 1c, and not written to disk. Otherwise the bytes are written and the hash is recorded. `_live_holder` does not trust `exists()`: it requires an ordinary file of the recorded size whose bytes hash to the recorded value (on case-insensitive APFS a claim on `Sample output.txt` is "satisfied" by a different file called `sample output.txt`). Recorded paths that fail the check are dropped from the entry, which is how a stale index heals itself.

The index maps hash to the paths that hold those bytes (`files[sha]["paths"]`); a duplicate's own path is not listed there, only in `dups`. `forget_hash_path` and `record_hash_path` compare paths case-insensitively, so replacing `sample output.txt` also clears a stale claim recorded as `Sample output.txt`.

**State persistence:** The hash index lives at `{output_dir}/.course_sync/downloaded.json`. It is loaded once at the start of `main()` and saved after each course via `save_hash_index()`, in a `finally`, so it is saved even when the course raised or had failures. The save is atomic: bytes are written to a `.tmp` file first, then `os.replace()` moves it over the final path, avoiding a partial write. A `downloaded.json` that is not valid JSON, not valid UTF-8 or not shaped like an index is never silently discarded: `load_hash_index` renames it to `downloaded.json.corrupt-<UTC timestamp>`, prints a warning to stderr and starts from an empty index (under `--dry-run` it only warns and leaves the file, since a dry run writes nothing).

**Atomic file writes:** `_write_bytes` writes a downloaded file to `<name>.part` and renames it, so a failure part-way cannot leave a truncated file that tier 1b would later trust.

**Partial-run recovery:** Because the hash index is saved per-course (not per-run), a run interrupted between courses retains a valid index for completed courses. The next run resumes from where it left off with no user action required.

**Memory note:** The full content of each downloaded file is buffered in memory before writing (`body = r.content`). For large files (e.g. recorded lecture slides) this means the peak memory footprint is roughly the size of the largest file. This is acceptable for typical course file sizes; for very large files it may be a consideration.

---

## 6. URL conversion and the double-webservice bug

Moodle's API returns pluginfile URLs in two forms. The authenticated-download form includes `/webservice/pluginfile.php/` in the path; the unauthenticated form uses `/pluginfile.php/` without the webservice prefix. The download endpoint requires the webservice form with a token appended.

`make_download_url` handles conversion and is also the last line of defence for the token (see [section 6b](#6b-token-safety-and-error-handling)); it raises `ValueError` unless `is_moodle_file_url(url, base_url)` holds:

```python
def make_download_url(url: str, token: str, base_url: str) -> str:
    if not is_moodle_file_url(url, base_url):
        raise ValueError(...)  # never attach the token to another host
    if "/webservice/pluginfile.php/" not in url:
        dl_url = url.replace("/pluginfile.php/", "/webservice/pluginfile.php/")
    else:
        dl_url = url
    if "token=" not in dl_url:
        sep = "&" if "?" in dl_url else "?"
        dl_url = f"{dl_url}{sep}token={token}"
    return dl_url
```

The guard `if "/webservice/pluginfile.php/" not in url` is critical. Without it, a URL that already contains `/webservice/pluginfile.php/` would have `/pluginfile.php/` replaced a second time, producing `/webservice/webservice/pluginfile.php/`, which returns a 404. This double-webservice URL was encountered in testing and is why the check exists. Do not simplify this function without verifying against real API responses from at least two different Moodle versions.

---

## 6b. Token safety and error handling

The Moodle token travels in the query string of every file request, and `requests` copies the full URL into the text of its exceptions. These rules keep it out of logs, generated files and other people's servers.

**Where the token may go.** Only to the configured Moodle site. `is_moodle_file_url(url, base_url)` is true only when the URL's scheme, hostname and port equal `moodle.base_url`'s and its path contains `/pluginfile.php/`. The label and url handlers use it to decide between "download this" and "record as an external link", so a `pluginfile.php` link on another host (another university's Moodle) is only ever an external link. `make_download_url` repeats the test and raises `ValueError`, so a future caller cannot bypass the handlers. Relative links are not resolved against `base_url`; they are ignored as before.

**Redirects.** `fetch_moodle_file(base_url, token, url, timeout)` is the one function that makes tokenised file requests. It sends `allow_redirects=False` and follows at most `MAX_REDIRECTS` (5) hops itself, and only to another pluginfile URL on the same scheme, host and port. A redirect anywhere else (another host, plain http after https, a login page) raises `RuntimeError`, which the caller logs as `[fail]`; the other host is never contacted. `handle_unit_guide` keeps `allow_redirects=True` because its requests carry no token.

**Redaction.** `register_secret(token)` is called by `api()` and `make_download_url()`. `redact(text)` replaces every registered value (also URL-quoted) and the value of any `token=` / `wstoken=` query parameter with `***`; `describe_error(exc)` gives `ClassName: message` through `redact`. Everything that reaches stdout, stderr or a generated file goes through one of them: `[fail]` lines, `Skipped` entries in `_index.md`, book chapter placeholders, `api()` errors (which are also raised `from None` so the original, token-bearing exception is not chained), the authentication failure message and the `--setup` connection error. `_content.md`, `_links.md`, page and book Markdown and `_index.md` additionally pass through `strip_secrets` (the known token value only, so a link or text that legitimately carries some other `token=` parameter is not rewritten); only the `Skipped` lines of `_index.md` use the full `redact`. `api()` has a 60 s timeout. A `requests.Session` was not added: tests and tooling count or fake `requests.get`, which a bound `Session.get` would bypass.

**Uncaught exceptions.** `main()` is `run_guarded(_main)`: anything uncaught prints `Error: <ClassName>: <redacted message>` to stderr and exits 1 instead of a traceback. `COURSE_SYNC_DEBUG=1` also prints the traceback, redacted. Ctrl-C exits 130.

**One failure does not stop the run.** Failures are caught at three levels, innermost first:

1. `_place_file` catches a failing file (`download_file` raised) and calls `record_failure(section_state, <path>, <reason>)`; the module's other files carry on.
2. `sync_course` catches anything a handler raises, per module, and calls `record_failure` with `<module name> (<modname>)`. It marks the section `incomplete`, which stops a stale `_links.md` from being removed (the module may have collected only some links).
3. `main` catches anything left, per course. A failing `core_course_get_contents` skips only that course.

`record_failure` prints `[fail] <label>: <reason>` and appends to the section's `skipped` list (so `_index.md` lists it under `Skipped` as `<label> - FAILED: <reason>`) and `failures` list. The section's `_index.md`, `_links.md` and `_content.md` are still written, and `main` saves the hash index after every course in a `finally`. `sync_course` returns the number of failures; `main` prints `Done, with N failure(s)` and exits 1 if there were any (so `sync_all.sh` warns), else prints `Done.` and exits 0. Warnings (`[warn]`: unsupported module types, a unit guide that cannot be matched) are not failures.

---

## 7. Filename sanitization

```python
def sanitize_filename(name: str) -> str:
    if not name:
        return "untitled"
    cleaned = re.sub(r'[:?*|<>\\/]', '', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = cleaned.rstrip('.')
    cleaned = cleaned.strip()
    return cleaned if cleaned else "untitled"
```

Characters removed: `:`, `?`, `*`, `|`, `<`, `>`, `\`, `/`

These are illegal or problematic in filenames on Windows (`:`, `?`, `*`, `|`, `<`, `>`, `\`) or as path separators (`/`). The backslash is also removed for consistency.

Additional normalization:
- Internal whitespace runs collapsed to single spaces
- Leading/trailing whitespace trimmed
- Trailing dots stripped (Windows does not allow filenames ending with a dot)
- Empty result falls back to `"untitled"`

Filenames returned by the Moodle API may also be URL-encoded. These are decoded with `urllib.parse.unquote` before passing to `sanitize_filename`.

**Length.** A name longer than `MAX_NAME_BYTES` (200) UTF-8 bytes would make the write fail with `OSError [Errno 63] File name too long` (limit 255 bytes; the margin leaves room for `.md`, ` (N)` and `.part`). `sanitize_filename` therefore cuts it with `_shorten_name`: the stem is truncated on a character boundary, a short extension (at most 16 bytes, no spaces) is kept, and `~<8 hex>` (the SHA-256 of the whole cleaned name) goes before the extension, so `Very long ... A.pdf` and `Very long ... B.pdf` stay distinct. The result is deterministic, and sanitising it again changes nothing. Folder names from `section_folder_name` go through the same function.

---

## 7b. Filename collisions

Two different files can have the same name in one folder: the same name from two modules, a name that differs only by case (`sample output.txt` / `Sample output.txt`, which are one path on case-insensitive APFS), two pages called "Spec", or names that only become equal after `sanitize_filename`. Writing both to the same path loses one, and tier 1b used to treat the second as "a stale copy of the first" and overwrite it back and forth on every run.

`claim_name(section_state, dest_dir, filename, source_key)` reserves names per destination folder, comparing them case-insensitively on every filesystem (`unicodedata.normalize("NFC", name).casefold()`), so behaviour is the same on APFS and on Linux. Rules:

- The first source to claim a name keeps it exactly as the server spelled it.
- A different source whose name matches gets `<stem> (2)<ext>`, then `(3)`, and so on, with the number inserted before the last extension (`archive.tar.gz` becomes `archive.tar (2).gz`).
- The same source claiming again gets the same name back. A source is the file behind a URL (`file_source_key`: host and pluginfile path, ignoring the query and the `/webservice` prefix, so two spellings of one URL are one file) or, for pages and books, the module (`cm:<id>`).
- `_index.md`, `_links.md` and `_content.md` are never handed out.
- The claims live in `section_state["name_claims"]`, one dict per course shared by all its sections, because several sections can write into the same folder (`Assessments/`). A file whose URL was already seen this run (tier 1a) does not claim again. Locked or empty books claim nothing.

Numbering follows the order the server lists modules, so names are stable from run to run for as long as that order is. If a lecturer reorders two clashing modules the two files swap names once: each is re-fetched into its new path (`[upd]`) and nothing is lost. A clashing file whose bytes equal the first one's is simply a `[dup]` (recorded, so it costs one fetch, once).

`sync_one.py` rebuilds the claims a full sync would hold by replaying the modules before the target as a silent dry run (`prime_name_claims`), so it picks the same name.

**Migrating a checkout that already hit the bug.** Disk has one file (`sample output.txt`, with the other file's bytes) and the index maps both hashes to the two spellings. The first run after upgrading logs `[upd]` for the first module's file (3186 -> 1107 bytes), `[dl]` for the second as `Sample output (2).txt`, and removes the stale index claims; the second run logs neither. `tests/test_name_collisions.py::MigrationTests` seeds exactly this state.

---

## 8. Audit files: _index.md and _links.md

Both files are written (or overwritten) at the end of each section's processing, inside `sync_course`. They are not appended; each run produces a fresh copy.

**`_index.md`**: Written by `write_section_index()`. Written only if at least one of `rows`, `external_links`, or `skipped` in `section_state` is non-empty. Contains:

- Header with section label and "auto-generated by course-sync"
- Last sync timestamp (local time)
- A Markdown table: Source module | Type | File | Destination
- External links summary (links to `_links.md` if present)
- Skipped items list: files dropped by `skip_patterns`, unsupported module types, locked books, and every failure as `<path or module> - FAILED: <reason>` (redacted)

Pipe characters in cell values are escaped with `\|` to avoid breaking the table. The File column shows the name actually used, so a file renamed by `claim_name` appears as `name (2).ext`.

**`_links.md`**: Written by `write_links_file()`. Written only if `external_links` in `section_state` is non-empty. Contains a Markdown list of `[display_text](url)` entries. When a section has no external links, a `_links.md` that this tool generated earlier is removed (`[links] ... removed`; `--dry-run` only reports it), because `sway_sync` discovers decks from these files. Only a file that is exactly in the generated format (the `# External links - ` header, then `- [text](url)` lines) is removed, so a hand-written or edited file is left alone. Nothing is removed when a module failed in that section (its link list may be incomplete). Removals are decided after the course's last section, so a section that has no links cannot delete a `_links.md` written by another section of the same run that maps to the same folder (e.g. `Week5_StudyWeek/`, `Assessments/`), whichever of them comes first.

Neither file is tracked in the hash index; they are always overwritten regardless of content.

**`_links.md` is also an interface.** `sway_sync.py` scans every `_links.md` under the configured unit folders for `[title](https://sway.cloud.microsoft/<id>)` lines and exports those decks. Keep the `- [display](url)` line format stable, or update `SWAY_LINK_RE` in `sway_sync.py` with it (see [Companion tools](#companion-tools-and-the-contract-between-them)).

---

## 9. Adding a new module handler

To add support for a new Moodle module type:

1. **Identify the modname.** Moodle's module type string (e.g. `assign`, `forum`, `h5pactivity`). Check what fields come back from `core_course_get_contents` for that module type by running a real API call or checking Moodle's web service documentation.

2. **Write the handler function** with the same signature as existing handlers:

   ```python
   def handle_mymodule(
       base_url: str,
       token: str,
       mod: dict,
       section_dir: Path,
       dry_run: bool,
       downloaded_urls: Set[str],
       output_dir: Path,
       hash_index: dict,
       section_state: dict,
   ) -> int:
       """
       Handle modname=mymodule. Returns count of files downloaded.
       """
       ...
   ```

   - For any file download, call `_place_file()` (skip rules, `_misc/` and `LessonN/` routing, `download_file()` and the index rows in one call) rather than repeating that sequence. Call `download_file()` directly only if the module needs different placement.
   - For a module that is text rather than files (like `page` and `book`), render it with `html_to_markdown()` and write it with `_write_text_if_changed()` so reruns are idempotent, and append its row with `_row()`.
   - Otherwise append rows to `section_state["rows"]` for each file processed (use `_row()`). `section_state` is the dict from `new_section_state()`.
   - Append non-downloadable URLs to `section_state["external_links"]` as `(display_text, url)` tuples.
   - Return the count of files where `outcome == "downloaded"`.

3. **Add to the dispatch in `dispatch_module`,** above the final `return`. That last line is `handle_unsupported_module`, the catch-all that warns about any type without a handler; adding your branch is what stops it firing for `mymodule`. `sync_one.py` uses the same function, so it picks the new type up too (extend `SECTION_ONLY_REASON` in `sync_one.py` if the module's output lives only in the section files):

   ```python
   if modname == "mymodule":
       return handle_mymodule(
           base_url, token, mod, section_dir, dry_run, downloaded_urls,
           output_dir, hash_index, section_state,
       )
   ```

   Let exceptions escape the handler for anything unexpected: `sync_course` isolates them per module. Catch them yourself only where you can do better, and then report through `record_failure()`.

4. **Test** using the protocol in [Testing protocol](#11-testing-protocol).

5. **Update `MOODLE_API.md`** if the new handler reads API fields not already documented there.

---

## 10. Out of scope (intentional)

These were explicitly excluded to keep the tool narrow and maintainable:

**PDF hyperlink extraction**: PDFs would need a third-party library (`pdfminer`, `pypdf`, etc.). The set of links inside a PDF is highly variable, and the tool would have no way to distinguish Moodle-hosted files from arbitrary external URLs. Excluded to avoid dependency creep.

**Forum content**: Forum threads are not files. They require separate API functions and a completely different output format. Out of scope by design.

**Quiz and assignment question/submission content**: Quiz and assignment *descriptions* are now captured as Markdown in `_content.md` (see [Section 3: Module type handlers](#3-module-type-handlers)). However, the actual quiz questions, answer choices, attempt data, and assignment submission files are stored in separate API functions and are out of scope.

**H5P and SCORM internals**: These are interactive packages. Downloading the container file is already handled (it appears as a `resource` module). Extracting or running the package contents is a different problem.

**Embedded videos (Echo360, Panopto, YouTube)**: These are served from external platforms behind their own authentication. The tool records these as external links in `_links.md`.

**Full fidelity for `page` and `book` bodies**: These are captured as Markdown via `markdownify` (see `save_page_markdown`, `handle_book_module`). Tables and embedded images lose fidelity, which the header of each generated file says. Files attached to a page are downloaded as ordinary files. Chapter-level attachments inside a book are not.

**Sidebar / theme blocks (Studiosity, Leganto, Unit Contacts, etc.)**: The Moodle theme renders these in a right-hand sidebar via a different web service (`core_block_*`) and they do not appear in `core_course_get_contents` output. They will not appear in `_content.md`. Out of scope.

If any of these need to be added, create a separate issue or discuss before implementing. Scope creep in a single-file tool quickly makes it unmaintainable.

---

## 11. Testing protocol

The automated suite ([Tests](#tests)) covers the logic with no network. It cannot replace a look at a real site, so after it passes, follow this manual workflow before merging a change that touches downloading or output:

1. **Dry run against a real Moodle instance.** Run `python3 src/course_sync.py --dry-run` with a config pointing at a real course. Confirm there are no Python errors and that the output lines look sensible.

2. **Real run.** Remove `--dry-run`. Check:
   - `[dl]` lines appear for expected files.
   - Files exist on disk at the logged paths.
   - `_index.md` is present in at least one section folder and its table matches the files on disk.
   - `.course_sync/downloaded.json` has grown and contains the SHA-256 hashes of downloaded files.

3. **Re-run.** Run the sync again immediately. Confirm:
   - Zero `[dl]`, `[upd]` and `[fail]` lines, and exit status 0.
   - All previously downloaded files appear as `[skip]`; content duplicates appear as `[dup] ... recorded, not re-fetched` (no GET).
   - `.course_sync/downloaded.json` has not changed (same content, same size).

4. **Dry run again after real run.** Confirm dry-run output is consistent with what's on disk.

5. **Edge case: duplicate file.** If possible, test with a course where the same file appears in multiple modules or sections. Confirm the second encounter logs `[dup]` with the first-known path, and that the run after it logs `[dup] ... recorded, not re-fetched`.

6. **Edge case: unsupported module.** Confirm every module type without a handler prints a `[warn] unsupported module type` line and appears under `Skipped` in that section's `_index.md`.

7. **Single module.** `python3 src/sync_one.py <cmid> --dry-run` (cmid is the `id=` number in the module's Moodle URL) should report the same target folder a full sync uses for that module, and `python3 src/sync_one.py <cmid>` must leave `_index.md`, `_links.md` and `_content.md` untouched. For a quiz, assign, label with no files or external link it must say nothing was written and exit 3.

8. **Sway decks (if you use them).** After a `course_sync.py` run, `.venv-sway/bin/python src/sway_sync.py --check` should list the decks linked from the `_links.md` files and write nothing (exit 0 if all unchanged, 3 if it found new or changed decks).

9. **Preview everything.** `./sync_all.sh --dry-run` must write nothing; its summary line says whether Sway found changes.

### Checking a refactor

For a change that should not alter behaviour, run the old and new code against the same inputs and diff the results rather than eyeballing them: point a temporary config's `output_dir` at an empty folder for each version, run `--dry-run`, then a real run, then a re-run, and compare stdout and the resulting trees. Ignore the `_Last sync:_` timestamp lines in `_index.md` / `_content.md`, which change every minute. Because the HTTP calls all go through `requests.get`, you can also replace it with a stub that serves a saved `core_course_get_contents` response and deterministic file bytes, which makes the comparison exact and works offline.

---

## Tests

An automated suite lives in `tests/` (stdlib `unittest` only, no pytest, no network, synthetic data only). From the repo root, with the interpreter that has the Moodle client's dependencies:

```
.venv/bin/python -B -m unittest discover -s tests -v
```

`tests/support.py` provides `FakeMoodle`, an in-memory Moodle site that replaces `requests.get` for the duration of a test (so a call that escapes it fails instead of touching the network), and `SyncCase`, which gives each test a scratch output dir and config and runs `course_sync.main()` against the fake. The fake token is `FAKE_TOKEN_abc123`; `assert_no_token_anywhere` scans every generated file and the captured output for it.

| File | What it pins down |
|---|---|
| `test_token_safety.py` | `redact`; `api()` errors, timeout and chain; no token in any file, stdout or stderr after failed fetches, an uncaught error, the auth failure and the `--setup` error; a foreign-host `pluginfile.php` link is never fetched or tokenised; `make_download_url` refuses other origins; a cross-host, downgrade, login-page or looping redirect is refused |
| `test_failure_isolation.py` | a 404, timeout or `OSError` mid-course: other files, other modules, the next course, the index files and the hash index all still happen; exit status 1; `[fail]` lines; book chapter `[keep]`; `_write_bytes` leaves no partial file |
| `test_long_names.py` | `sanitize_filename` length cap, extension, hash suffix, UTF-8 boundary, idempotence; a 300-character file, page, book and section name sync and re-sync cleanly |
| `test_name_collisions.py` | `claim_name`; case variants, identical names, pages and books, shared folders, generated names; the migration of the case-collision state from the live checkout, in both module orders, with a stable second run |
| `test_replaced_files.py` | an identical-content re-upload is re-fetched once, then its mtime is stamped; a later real change is still picked up |
| `test_unit_guide.py` | `pdf_fingerprint`; a re-render differing only in `/ID` or dates is not rewritten and the index records the on-disk hash; a real edit is written |
| `test_dup_records.py` | duplicates cost one GET ever; the record's fields; invalidation by changed metadata, a deleted or replaced original, a real file at the path; label links without metadata; legacy index cleanup |
| `test_robustness.py` | corrupt `downloaded.json` is quarantined with a warning (only warned about under `--dry-run`); a stale generated `_links.md` is removed, an edited one and one written by a sibling section (in either order) are not |
| `test_sync_one.py` | every module type goes through `dispatch_module`; exit codes 0/1/2/3; section files untouched; clash names match a full sync |
| `test_sync_all_sh.py` | `sync_all.sh` with stub interpreters: `--dry-run` runs `--dry-run` and `--check`, sway exit 3 is informational, unknown arguments exit 2, and the no-argument output is byte-identical to the previous script |

When you fix a bug, add a test that fails without the fix. Keep fixtures synthetic: no real course names, no files from `audit/` or a live workspace.

---

## 12. Contributing

- Fork the repo, create a branch, open a pull request.
- Keep Python 3.9 compatibility. Do not use syntax or stdlib features added in 3.10+.
- Do not add new dependencies without strong justification. The current dependencies of `course_sync.py` are `requests`, `PyYAML` and `markdownify` (see `requirements/course_sync.txt`). Any new dep should be justified in the same way.
- Keep `course_sync.py` a single file. Do not split it into a package unless the scope expands substantially. Extract repeated logic into shared functions within the file instead (as `_place_file` does).
- `sway_sync.py` has its own dependency (`requirements/sway_sync.txt`) and its own venv. It must not import `course_sync`, and anything it needs from `config.yaml` must work without PyYAML.
- Run the tools from the repo root and keep them in `src/`: both compute the repo root as `Path(__file__).resolve().parents[1]`, so moving a script to a different depth changes where it looks for `config.yaml`, its state files and its venv (see [Companion tools](#companion-tools-and-the-contract-between-them)). Don't name any directory `course_sync/` next to them, or `import course_sync` breaks.
- Do not include AI co-author lines (e.g., `Co-Authored-By: ...`) in commit messages. This is a project convention.
- Run the [automated tests](#tests), then the manual protocol in [Testing protocol](#11-testing-protocol), before opening a pull request. Add a test with every behaviour change.
- For significant scope changes (new module handlers, new output formats, new config keys), open an issue for discussion first.

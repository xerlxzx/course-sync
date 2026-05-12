# Developer Guide

This guide covers the internals of `course_sync.py` for contributors and developers who want to understand, extend, or debug the tool (part of the `course-sync` project). It assumes Python familiarity but not Moodle-specific knowledge.

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
7. [Filename sanitization](#7-filename-sanitization)
8. [Audit files: _index.md and _links.md](#8-audit-files-_indexmd-and-_linksmd)
9. [Adding a new module handler](#9-adding-a-new-module-handler)
10. [Out of scope (intentional)](#10-out-of-scope-intentional)
11. [Testing protocol](#11-testing-protocol)
12. [Contributing](#12-contributing)

---

## 1. Architecture overview

The entire implementation lives in a single file: `course_sync.py`. This is a deliberate choice: the tool has a narrow scope, and a single file is easier for students and casual contributors to read, copy, or fork than a package with multiple modules. No packaging, no `setup.py`, no entry points.

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
            -> map section name to folder (Week N / Assessments / skip)
            -> for each module in section:
                -> dispatch to handler by modname
                -> handler extracts file URLs, calls download_file()
                -> download_file() applies two-tier dedup, writes bytes
            -> write _links.md (if external links found)
            -> write _index.md (if any rows/links/skipped)
        save hash index (per course, not per run)
```

**Key source regions:**

| Content | Key symbols |
|---|---|
| API helpers | `api`, `get_user_id`, `get_courses`, `get_contents` |
| Folder/filename derivation | `section_to_week_folder`, `section_folder_name`, `lesson_subfolder_from_filename`, `is_assessment_section`, `sanitize_filename`, `collapse_ws` |
| URL form fix | `make_download_url`: double-webservice guard |
| Hash index | `load_hash_index`, `save_hash_index`, `record_hash_path` |
| File download | `download_file`: two-tier dedup |
| HTML extraction | `extract_links_from_html`, `_strip_tags` |
| HTML-to-Markdown | `html_to_markdown`, `extract_heading_from_html`, `is_content_block_skippable`, `clean_section_name`, `short_course_code` |
| Noise routing | `DEFAULT_MISC_PATTERNS`, `MISC_LABEL_NAME_SUBSTRINGS`, `is_misc_file`, `should_skip_file`, `label_is_misc`, `_classify_misc_reason` |
| Module handlers | `handle_contents_module`, `handle_label_module`, `handle_url_module` |
| Unit guide | `_is_unit_guide_url`, `handle_unit_guide` |
| Section output | `write_links_file`, `write_section_content`, `write_section_index` |
| Orchestration | `sync_course`, `main` |
| Config parsing | `load_config`, `resolve_path`, `load_token`, `parse_courses`, `parse_pattern_list` |

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

The dispatch logic is in `sync_course`:

```python
if modname in ("resource", "folder", "page"):
    handle_contents_module(...)
elif modname == "label":
    handle_label_module(...)
elif modname == "url":
    handle_url_module(...)
elif modname in ("quiz", "assign"):
    # Description captured as a content block; no file download.
    ...
```

Other `modname` values (e.g. `forum`, `h5pactivity`) are silently ignored.

### `quiz` and `assign` modules

These modules have no downloadable files via the content API. `sync_course` reads `mod["name"]` and `mod["description"]` for each matched module. The description HTML is converted to Markdown with `html_to_markdown()`; if the description is empty, a stub line (`*Quiz: <name>*` or `*Assign: <name>*`) is used. The result is appended to `section_state["content_blocks"]` under the module name as heading, then written to `_content.md` with the rest of the section's prose.

### `handle_contents_module` (resource / folder / page)

Iterates `mod["contents"]`, filtering to entries with `type == "file"`. For each file:

1. Reads `fileurl` and `filename` from the content entry.
2. Sanitizes the filename.
3. Checks for the Moodle page-template `index.html` (skipped with `[skip-html]`).
4. Applies the lesson-subfolder heuristic to determine destination.
5. Calls `download_file()`.
6. Appends a row to `section_state["rows"]` for `_index.md`.

### `handle_label_module` (label)

Label modules store content as HTML in `mod["description"]`. Calls `extract_links_from_html()` to find all `<a href>` and `<img src>` URLs in that HTML.

For each link found:

- If the URL contains `"pluginfile.php"`: treat as a downloadable file, extract the filename from the URL path, apply lesson-subfolder logic, call `download_file()`.
- If the URL starts with `"http"` but has no `pluginfile.php`: append to `section_state["external_links"]`.

This is the mechanism that catches the common Moodle pattern of embedding slide download links inside a text label.

### `handle_url_module` (url)

URL modules store their target in `mod["contents"][0]["fileurl"]`. If that URL contains `"pluginfile.php"`, it is downloaded like a file. Otherwise, the module's `name` is used as the display text and the URL is recorded as an external link.

### `extract_links_from_html`

Extracts `(url, display_text)` pairs from an HTML string. Processes anchor tags first, recording their span positions. Then processes `<img src>` tags, skipping any that fall inside an already-processed anchor span (to avoid double-counting images that are also links).

Excludes fragment-only URLs (`#...`) and `mailto:` links.

---

## 3b. Unit guide PDF download

Unit guide links appear as `url` modules in sections that the main loop would otherwise process normally (typically the "General" section). However, they are also pre-scanned in a separate pass before the main section loop so they are captured even in sections the main loop might otherwise skip.

**`_is_unit_guide_url(url: str) -> bool`**: Checks whether the URL's hostname contains `"unitguide"`. This heuristic catches Macquarie's `unitguides.mq.edu.au` and similar portals at other institutions that follow the same naming convention.

**`handle_unit_guide(fileurl, course_dir, dry_run, output_dir, hash_index, downloaded_urls) -> bool`**: Fetches the unit guide portal page, searches for a unit offering ID in the redirect URL or response body (`/unit_offerings/<ID>`), then constructs and downloads the printer-friendly PDF from `.../unit_offerings/<ID>/unit_guide/print.pdf`. The PDF is saved as `Unit_Guide.pdf` in the course root (not inside a weekly section folder). Returns `True` if a file was actually downloaded.

Failure modes (each prints a `[warn]` line and returns `False`):
- Network error fetching the portal page
- No offering ID found in the redirect URL or response body
- Network error downloading the PDF
- Response `Content-Type` does not contain `"pdf"`

The dedup index is applied; if the PDF's hash is already recorded, `[dup]` is logged.

---

## 3c. Section folder routing

`section_to_week_folder(name)` handles the `"Week N"` pattern. `is_assessment_section(name)` routes to `Assessments/`. All remaining sections fall through to `section_folder_name(sec_name, section_index)`.

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

Some files are part of the course but aren't lecture material: the MQ logo embedded in every page, the Wallumedegal land photo inside "Acknowledgement of Country" labels, assignment templates, etc. The tool routes these to a `_misc/` subfolder inside each section, and also supports a hard-skip list for files that shouldn't even be downloaded.

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

`sync_course` parses `misc_patterns` / `skip_patterns` once via `parse_pattern_list`, then forwards both lists to each handler. Each handler checks `should_skip_file` first; if false, computes a `misc_reason` (either via `is_misc_file` or, for labels, `label_is_misc`). When a reason is set, the destination becomes `section_dir / MISC_SUBFOLDER`, overriding lesson-folder detection. The handler logs `[misc]` and appends to `section_state["misc_rows"]` so `write_section_index` can render the new `Routed to _misc/` table.

### Precedence (in order)

For each candidate file, decisions run in this order:

1. **`[skip-html]`**: Moodle `page` module's `index.html` template (existing behaviour, unchanged).
2. **`[skip-rule]`**: basename matches `skip_patterns`. File is not downloaded. Recorded in `section_state["skipped"]`.
3. **`_misc/` routing**: basename matches a default `misc_patterns` entry, a user `misc_patterns` entry, OR (for labels) `label_is_misc(mod_name)` is true. Destination overrides the lesson-folder destination. Default patterns are checked before user patterns so the reason string in `_index.md` stays specific (`logo pattern` / `template pattern`) rather than the generic `matched misc_patterns`.
4. **Lesson-folder detection**: existing `lesson_subfolder_from_filename` heuristic.
5. **Section root**: fallthrough.

`_misc/` takes precedence over lesson detection: a logo file named `Lesson 2 Logo.png` lands in `_misc/`, not `Lesson2/`.

### Interaction with hash dedup

`_misc/` routing is decided *before* `download_file` is called, so the destination path is final by the time the two-tier dedup runs. On a fresh install, a misc file is fetched, hashed, and written into `_misc/` like any other file.

On an *upgrade* run (existing user, files already on disk under the pre-`_misc/` routing), the same file shows up as a `[dup]` against its old path: the bytes are fetched, hashed, found in `hash_index["files"]`, and the new `_misc/` path is recorded in the hash index but the file is not re-written. The `Routed to _misc/` table in `_index.md` still records what *would* land there, giving the user a clear migration audit. The user can then manually move the old file (or delete it and let a fresh sync rebuild); the dedup index will handle either gracefully.

This is intentional and matches the documented behaviour for any routing change: hash-dedup wins to avoid double-storing bytes; `_index.md` makes the disagreement visible.

---

## 4c. Rich-text capture: `_content.md`

Label descriptions, section summaries, and similar HTML fragments inside Moodle hold a non-trivial amount of prose that is *not* a file: assessment briefs, academic integrity statements, lesson framing, etc. Mining these for `<a href>` and `<img src>` was already in scope (see `handle_label_module`), but the readable prose itself was being discarded. The `_content.md` capture path preserves that prose as Markdown.

### Conversion

`html_to_markdown(html_str: str) -> str` runs the HTML through the third-party [`markdownify`](https://pypi.org/project/markdownify/) library with ATX headings and `-` bullets. Before conversion it strips the Moodle-added `<div class="no-overflow">` wrapper; after conversion it collapses 3+ blank lines to 2.

`markdownify` was chosen over rolling our own converter because real Moodle content contains HTML tables (the WSTA1250 Assessment summary is a 3-column table) and writing a robust table walker is more work than the rest of the feature combined. It is the first new dependency since `requests` and `PyYAML`; pure-Python; ~30 KB.

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

`sync_course` adds an empty `content_blocks: List[Tuple[str, str, str]]` to `section_state` before iterating modules. The section-summary capture runs first, skips acknowledgement-like boilerplate summaries, and appends a `("(heading)", "section summary", md)` entry when the remaining Markdown passes `is_content_block_skippable`. `handle_label_module` calls `is_content_block_skippable` and `extract_heading_from_html` after the existing link-extraction pass and appends `("(heading)", "label", md)` if the body passes. `write_section_content` reads the list, writes `_content.md` if non-empty, and returns whether it did so. The boolean is forwarded into `write_section_index` so the "Prose content" cross-reference renders.

### Names and entities

`clean_section_name(name)` runs `html.unescape` so `Week 1: Intro &amp; Hygiene` becomes `Week 1: Intro & Hygiene`. The decoded form is used everywhere the section name is shown: `_content.md` heading, `_index.md` heading, the per-section log line.

`short_course_code(shortname)` keeps the leading `[A-Za-z]+\d+` run of a Moodle shortname (e.g. `WSTA1250_MQUC2_2026_ALL_U` → `WSTA1250`). Used only for the `# Section - CODE` heading in `_content.md`. Falls through to returning the raw shortname if no letters-followed-by-digits prefix is found.

---

## 5. Dedup system

**Tier 1a (per-run URL set):** `downloaded_urls: Set[str]` is initialised per course and passed through to every `download_file()` call. If the same `fileurl` appears in multiple modules (a common occurrence when lecturers link the same file from multiple places), only the first encounter is processed.

**Tier 1b (path on disk):** If the output path already exists as a file, it is printed as `[skip]` and not re-fetched. This is the primary mechanism for fast re-runs.

**Tier 2 (SHA-256 hash index):** For files that pass both Tier 1 checks, the full file content is fetched into memory. A SHA-256 hash of the bytes is computed. If that hash already exists in `hash_index["files"]`, the file is a duplicate, logged as `[dup]` with the first-known path, and not written to disk. Otherwise the bytes are written and the hash is recorded.

**State persistence:** The hash index lives at `{output_dir}/.course_sync/downloaded.json`. It is loaded once at the start of `main()` and saved after each course via `save_hash_index()`. The save is atomic: bytes are written to a `.tmp` file first, then `os.replace()` moves it over the final path, avoiding a partial write.

**Partial-run recovery:** Because the hash index is saved per-course (not per-run), a run interrupted between courses retains a valid index for completed courses. The next run resumes from where it left off with no user action required.

**Memory note:** The full content of each downloaded file is buffered in memory before writing (`body = r.content`). For large files (e.g. recorded lecture slides) this means the peak memory footprint is roughly the size of the largest file. This is acceptable for typical course file sizes; for very large files it may be a consideration.

---

## 6. URL conversion and the double-webservice bug

Moodle's API returns pluginfile URLs in two forms. The authenticated-download form includes `/webservice/pluginfile.php/` in the path; the unauthenticated form uses `/pluginfile.php/` without the webservice prefix. The download endpoint requires the webservice form with a token appended.

`make_download_url` handles conversion:

```python
def make_download_url(url: str, token: str) -> str:
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

---

## 8. Audit files: _index.md and _links.md

Both files are written (or overwritten) at the end of each section's processing, inside `sync_course`. They are not appended; each run produces a fresh copy.

**`_index.md`**: Written by `write_section_index()`. Written only if at least one of `rows`, `external_links`, or `skipped` in `section_state` is non-empty. Contains:

- Header with section label and "auto-generated by course-sync"
- Last sync timestamp (local time)
- A Markdown table: Source module | Type | File | Destination
- External links summary (links to `_links.md` if present)
- Skipped items list

Pipe characters in cell values are escaped with `\|` to avoid breaking the table.

**`_links.md`**: Written by `write_links_file()`. Written only if `external_links` in `section_state` is non-empty. Contains a Markdown list of `[display_text](url)` entries.

Neither file is tracked in the hash index; they are always overwritten regardless of content.

---

## 9. Adding a new module handler

To add support for a new Moodle module type:

1. **Identify the modname.** Moodle's module type string (e.g. `assign`, `forum`, `h5pactivity`). Check what fields come back from `core_course_get_contents` for that module type by running a real API call or checking Moodle's web service documentation.

2. **Write the handler function** with the same signature as existing handlers:

   ```python
   def handle_mymodule(
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

   - Use `download_file()` for any file download.
   - Append rows to `section_state["rows"]` for each file processed (use `_row()`).
   - Append non-downloadable URLs to `section_state["external_links"]` as `(display_text, url)` tuples.
   - Return the count of files where `outcome == "downloaded"`.

3. **Add to the dispatch in `sync_course`:**

   ```python
   elif modname == "mymodule":
       downloaded += handle_mymodule(
           token, mod, section_dir, dry_run, downloaded_urls,
           output_dir, hash_index, section_state,
       )
   ```

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

**PDF/text content of Moodle `page` modules**: A `page` module is a rich-text editor page inside Moodle. The tool downloads files attached to the page (via `contents`). The rendered HTML body of the page itself is not extracted or saved.

**Sidebar / theme blocks (Studiosity, Leganto, Unit Contacts, etc.)**: The Moodle theme renders these in a right-hand sidebar via a different web service (`core_block_*`) and they do not appear in `core_course_get_contents` output. They will not appear in `_content.md`. Out of scope.

If any of these need to be added, create a separate issue or discuss before implementing. Scope creep in a single-file tool quickly makes it unmaintainable.

---

## 11. Testing protocol

There is no automated test suite yet. Until one exists, follow this manual workflow before merging any change:

1. **Dry run against a real Moodle instance.** Run `python3 course_sync.py --dry-run` with a config pointing at a real course. Confirm there are no Python errors and that the output lines look sensible.

2. **Real run.** Remove `--dry-run`. Check:
   - `[dl]` lines appear for expected files.
   - Files exist on disk at the logged paths.
   - `_index.md` is present in at least one section folder and its table matches the files on disk.
   - `.course_sync/downloaded.json` has grown and contains the SHA-256 hashes of downloaded files.

3. **Re-run.** Run the sync again immediately. Confirm:
   - Zero `[dl]` lines.
   - All previously downloaded files appear as `[skip]`.
   - `.course_sync/downloaded.json` has not changed (same content, same size).

4. **Dry run again after real run.** Confirm dry-run output is consistent with what's on disk.

5. **Edge case: duplicate file.** If possible, test with a course where the same file appears in multiple modules or sections. Confirm the second encounter logs `[dup]` with the first-known path.

---

## 12. Contributing

- Fork the repo, create a branch, open a pull request.
- Keep Python 3.9 compatibility. Do not use syntax or stdlib features added in 3.10+.
- Do not add new dependencies without strong justification. The current dependencies are `requests` and `PyYAML`, both near-universal. Any new dep should be equally justified.
- Keep the implementation in a single file. Do not split into a package unless the scope expands substantially.
- Do not include AI co-author lines (e.g., `Co-Authored-By: ...`) in commit messages. This is a project convention.
- Test using the protocol in [Testing protocol](#11-testing-protocol) before opening a pull request.
- For significant scope changes (new module handlers, new output formats, new config keys), open an issue for discussion first.

# course-sync

Downloads and organizes your course files into clean local folders. Works with **any university** that uses Moodle (3.9+).

## What it does

`course-sync` connects to your uni's course platform, pulls down every file your lecturers have uploaded, and sorts them into folders by week and lesson. Run it again later to grab anything new; it skips what you already have.

**Features:**

- Downloads PDFs, slides, spreadsheets, datasets, and other files from all sections
- Sorts files into `WeekN/` and `LessonN/` folders automatically
- Grabs the unit guide PDF if there's a link to it on the course page
- Saves assessment details, due dates, and quiz/assignment info as Markdown
- Pulls out course content embedded in page descriptions into readable Markdown files
- Collects external links (YouTube, Echo360, etc.) per section into `_links.md`
- Moves noise files (logos, templates, boilerplate images) into `_misc/` so they're out of the way
- Skips files you've already downloaded (SHA-256 dedup)
- Handles every section type, not just weekly ones (General, Study Resources, etc.)
- Captures Moodle `page` and `book` modules as Markdown, and warns loudly about module types it can't capture
- Dry-run mode to preview what would be downloaded
- Generates `_index.md` audit files so you can see exactly what was synced
- Shows your Moodle version on connect and warns if it's too old

## Works with

Any Moodle 3.9+ instance with mobile web services enabled. Universities often give their Moodle site its own name; if it says "Powered by Moodle" in the footer, it should work. All you need is the address you log into, e.g. `https://moodle.example.edu`.

## Quick start

```bash
# 1. Clone and install
git clone https://github.com/xerlxzx/course-sync course-sync
cd course-sync
pip install -r requirements/course_sync.txt

# 2. Run the setup wizard (walks you through everything)
python3 src/course_sync.py --setup

# 3. Preview, then download
python3 src/course_sync.py --dry-run    # see what would be downloaded
python3 src/course_sync.py              # download for real
```

You can also configure manually: copy `config.example.yaml` to `config.yaml` and fill in your details. See the [User Guide](docs/USER_GUIDE.md) for the full walkthrough.

## CLI

```
python3 src/course_sync.py [--config PATH] [--token TOKEN] [--dry-run] [--setup]
```

| Flag | Default | What it does |
|---|---|---|
| `--setup` | | Interactive wizard: enter URL, token, pick courses, generate config |
| `--config` | `./config.yaml` | Path to YAML config |
| `--token` | from `token_file` in config, then `MOODLE_TOKEN` env var | Overrides both |
| `--dry-run` | off | Preview without downloading |

Exit status: `0` when everything worked, `1` if anything failed (`[fail]` lines in the output) or the run could not start.

Other entry points, all run from the repo root:

| Command | What it does |
|---|---|
| `./sync_all.sh` | Moodle files, then Sway decks (the normal weekly run) |
| `./sync_all.sh --dry-run` | Preview of both: `course_sync.py --dry-run`, then `sway_sync.py --check`. Writes nothing. Sway's "changes found" (exit 3) is reported as a note, not an error. Other arguments are rejected (exit 2). |
| `python3 src/sync_one.py <cmid> [--dry-run]` | Re-fetch one module. Exit `0` handled, `1` error or cmid not found, `2` bad command line, `3` nothing written because the module's output lives only in `_content.md` / `_links.md` (quiz, assign, label text, external link, unsupported type), which this command never rewrites. |

## What gets downloaded

| Module type | What happens |
|---|---|
| `resource` | Direct file download (PDFs, slides, etc.) |
| `folder` | All files inside go into the section folder |
| `page` | Page body rendered to `<page name>.md`; files attached to the page are downloaded |
| `book` | All chapters rendered into one `<book name>.md` (locked or empty books are reported, not dropped) |
| `label` | Files linked from inside the label's HTML description |
| `url` | Downloaded if it points to a Moodle file, otherwise saved in `_links.md` |
| `quiz`, `assign` | Description and due-date info captured as Markdown in `_content.md` |

Noise files (logos, templates, Acknowledgement of Country imagery) get routed to a `_misc/` subfolder automatically. You can customize this with `misc_patterns` / `skip_patterns` in config.

Not handled: forum posts, H5P/SCORM content, embedded videos, links inside PDFs. Any module type without a handler prints a `[warn]` line and is listed under `Skipped` in the section's `_index.md`, so nothing disappears silently.

## Repository layout

```
course-sync/                    the repo root, which is also the runtime root
├── src/
│   ├── course_sync.py          Moodle downloader. One file, Python 3.9+. The main tool.
│   ├── sway_sync.py            Optional: exports Microsoft Sway lecture decks (Playwright, own venv)
│   └── sync_one.py             Maintenance: re-fetch a single module by its cmid
├── scripts/
│   └── sync_all.sh             Runs course_sync.py, then sway_sync.py (--dry-run previews both)
├── tests/                      Automated tests (stdlib unittest, no network); see "Running the tests"
├── requirements/
│   ├── course_sync.txt         Dependencies for course_sync.py and sync_one.py
│   └── sway_sync.txt           Dependencies for sway_sync.py (separate venv)
├── docs/                       USER_GUIDE, DEV_GUIDE, MOODLE_API, SWAY_SYNC
├── sync_all.sh                 Shim: execs scripts/sync_all.sh (for launchers that run ./sync_all.sh)
├── config.example.yaml         Copy to config.yaml
├── LICENSE
└── README.md
```

Code lives in `src/` and scripts in `scripts/`, but the repo root is where the tools run and keep their files. Each tool works out the root as the parent of `src/`, and finds `config.yaml`, `.course_sync_token`, `.sway-auth.json`, `.sway-profile/`, `.sway-state.json` and the `.venv*/` interpreters there. Relative paths inside `config.yaml` (`token_file`, `output_dir`) resolve against it too. The commands in this README are run from the repo root. `src/` is deliberately not named `course_sync/`: `sync_one.py` does `import course_sync`, and a directory of that name would shadow the file.

**Local-only files** (all gitignored, all in the repo root unless noted): `config.yaml`, `.course_sync_token`, `.sway-auth.json`, `.sway-profile/`, `.sway-state.json`, `.venv/`, `.venv-sway/`, and any `audit/` folder of raw API dumps. Downloads go to `output_dir` (`.course_sync/downloaded.json`, the dedup index, lives there too, not in the repo).

**How the tools fit together:** `course_sync.py` writes a `_links.md` per section; `sway_sync.py` finds Sway deck URLs in those files, so it must run second. Both read `config.yaml` (`courses[].folder` and `output_dir`), so adding a unit means editing only that file. `src/sync_one.py` imports `course_sync.py` (they sit side by side in `src/`); `sway_sync.py` deliberately does not, because it runs in a different virtualenv. See [Developer Guide, section 1](docs/DEV_GUIDE.md#1-architecture-overview).

## Documentation

- **[User Guide](docs/USER_GUIDE.md)** - setup, config, output structure, troubleshooting, FAQ
- **[Developer Guide](docs/DEV_GUIDE.md)** - architecture, handler internals, dedup, contributing
- **[Moodle API Reference](docs/MOODLE_API.md)** - endpoints, fields, failure modes
- **[Sway deck exports](docs/SWAY_SYNC.md)** - optional: setup and use of `sway_sync.py`

---

## Technical reference

### Architecture

The Moodle downloader lives in one file, `course_sync.py`. Key functions:

| Function | What it does |
|---|---|
| `api()` | Moodle REST caller (60 s timeout, token-free errors) |
| `redact()` / `describe_error()` | Strip the token from anything printed or written |
| `is_moodle_file_url()` / `make_download_url()` / `fetch_moodle_file()` | The token gate: only your Moodle host, same-site redirects only |
| `claim_name()` | Keeps files whose names clash (including by case) as `name (2).ext` |
| `section_to_week_folder()` | Maps "Week 3 - ..." to `Week3` folder |
| `section_folder_name()` | Maps non-week, non-assessment sections to folder names |
| `section_dir_for()` | Single routing rule (assessment, then week, then fallback) used by `sync_course` and `sync_one.py` |
| `lesson_subfolder_from_filename()` | Detects `LessonN/` from filename |
| `is_assessment_section()` | Routes section to `Assessments/` |
| `_is_unit_guide_url()` | Detects unit guide portal URLs |
| `handle_unit_guide()` | Downloads unit guide PDF to course root (`pdf_fingerprint()` ignores the per-render `/ID` and dates) |
| `select_unit_offering()` | Picks the unit offering ID from a unit-guide response |
| `handle_contents_module()` | Handles `resource`, `folder`, `page` modules |
| `save_page_markdown()` | Renders a `page` module's body to Markdown |
| `handle_book_module()` | Renders a `book` module's chapters to one Markdown file |
| `handle_label_module()` | Extracts files and prose from `label` HTML |
| `handle_url_module()` | Handles `url` modules |
| `handle_description_module()` | Captures `quiz` / `assign` descriptions as `_content.md` blocks |
| `handle_unsupported_module()` | Catch-all: warns, records under `Skipped`, keeps any description text |
| `dispatch_module()` | Routes one module to its handler by `modname`; shared by `sync_course` and `sync_one.py` |
| `_place_file()` | Shared by the file handlers: skip rules, `_misc/` / `LessonN/` routing, name claim, download (a failure becomes `[fail]`), index rows |
| `html_to_markdown()` | Converts Moodle HTML to Markdown via `markdownify` |
| `download_file()` | Tiered dedup (path on disk, recorded duplicates, hash index), atomic writes |
| `sync_course()` | Per-course orchestrator; returns the number of failures |
| `run_setup()` | Interactive setup wizard |
| `main()` | CLI entry (`run_guarded`: one redacted error line, never a traceback), config loading, course matching, exit status |

### Config reference

```yaml
moodle:
  base_url: https://your-moodle-site.edu    # required, no trailing slash
  token_file: .course_sync_token                  # path to token file

output_dir: ~/Documents/Courses              # where files go

courses:
  - code: COMP1000                           # prefix of Moodle course shortname
    folder: COMP1000                         # local folder name (defaults to code)

# Optional
log_file: sync.log                           # not wired up yet; output goes to stdout
misc_patterns:                               # extend built-in noise patterns
  - "(?i)welcome.*image"
skip_patterns:                               # skip these files entirely
  - "(?i)deprecated"
```

| Key | Required | Description |
|---|---|---|
| `moodle.base_url` | Yes | Your Moodle URL, no trailing slash |
| `moodle.token_file` | Yes* | Path to token file. *Or use `--token` / `MOODLE_TOKEN` env var |
| `output_dir` | Yes | Where course folders are created. Absolute or `~`-prefixed, or relative to the config file's folder (`..` is the folder above the repo). `sway_sync.py` reads this too |
| `courses[].code` | Yes | Prefix matched against enrolled course shortname |
| `courses[].folder` | No | Local folder name. Defaults to `code`. `sway_sync.py` scans these folders for Sway links |
| `log_file` | No | Declared but output currently goes to stdout |
| `misc_patterns` | No | Regex patterns for files to route to `_misc/`. Extends defaults |
| `skip_patterns` | No | Regex patterns for files to skip entirely |

### How deduplication works

Tiers, in order:

1. **Per-run URL set + path on disk.** If the URL was already hit this run, or the file already exists and the server's size and modified time say it is current, it's skipped (`[skip]`). A re-upload with identical bytes is fetched once and the local file is stamped, so it is not fetched again.
2. **Recorded duplicates.** If an earlier run found that a file's content is already on disk under another path, the index remembers it (with the server's size and time). While neither side has changed it is reported as `[dup]` and not fetched.
3. **SHA-256 hash index.** If the file isn't on disk but its content matches something already downloaded (and that copy verifies on disk), it's logged as `[dup]`, recorded as in 2, and not written again. The index lives at `{output_dir}/.course_sync/downloaded.json` and saves after each course, even a failed one, so partial runs don't lose progress. A corrupt index is renamed to `downloaded.json.corrupt-<date>` with a warning, not silently discarded.

Different files that would share a name in one folder (including names that differ only by case) are kept: the later ones are saved as `name (2).ext`, `name (3).ext`, in the order Moodle lists them.

Dry-run doesn't fetch file bytes, so it can't discover new duplicates; it does report the ones already recorded.

### Safety and failures

The token is sent only to your configured Moodle host and never follows a redirect to another host; links to files on any other host are listed in `_links.md` instead of downloaded. Error messages, `_index.md` and every other generated file have the token removed. One failing file, module or course does not stop the run: it is logged as `[fail]`, listed under `Skipped` in the section's `_index.md`, and the exit status is 1 at the end. See [Developer Guide, section 6b](docs/DEV_GUIDE.md#6b-token-safety-and-error-handling).

### Running the tests

```
.venv/bin/python -B -m unittest discover -s tests -v
```

Standard-library `unittest` only; no network and no real credentials are used (a fake Moodle server replaces `requests.get`). Details in [Developer Guide, Tests](docs/DEV_GUIDE.md#tests).

## License

MIT. See [LICENSE](LICENSE).

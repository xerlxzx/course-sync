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
- Dry-run mode to preview what would be downloaded
- Generates `_index.md` audit files so you can see exactly what was synced
- Shows your Moodle version on connect and warns if it's too old

## Works with

Any Moodle 3.9+ instance with mobile web services enabled. Tested at:

| University | URL | Platform name |
|---|---|---|
| UNSW | `https://moodle.telt.unsw.edu.au` | Moodle |
| Macquarie University | `https://ilearn.mq.edu.au` | iLearn |
| Monash University | `https://learning.monash.edu` | Moodle |
| Any other Moodle 3.9+ site | your uni's Moodle URL | varies |

If you've got it working at your uni, open an issue or PR to add it to the list.

## Quick start

```bash
# 1. Clone and install
git clone https://github.com/xerlxzx/course-sync course-sync
cd course-sync
pip install -r requirements.txt

# 2. Run the setup wizard (walks you through everything)
python3 course_sync.py --setup

# 3. Preview, then download
python3 course_sync.py --dry-run    # see what would be downloaded
python3 course_sync.py              # download for real
```

You can also configure manually: copy `config.example.yaml` to `config.yaml` and fill in your details. See the [User Guide](docs/USER_GUIDE.md) for the full walkthrough.

## CLI

```
python3 course_sync.py [--config PATH] [--token TOKEN] [--dry-run] [--setup]
```

| Flag | Default | What it does |
|---|---|---|
| `--setup` | | Interactive wizard: enter URL, token, pick courses, generate config |
| `--config` | `./config.yaml` | Path to YAML config |
| `--token` | from `token_file` in config, then `MOODLE_TOKEN` env var | Overrides both |
| `--dry-run` | off | Preview without downloading |

## What gets downloaded

| Module type | What happens |
|---|---|
| `resource` | Direct file download (PDFs, slides, etc.) |
| `folder` | All files inside go into the section folder |
| `page` | Files attached to the page resource |
| `label` | Files linked from inside the label's HTML description |
| `url` | Downloaded if it points to a Moodle file, otherwise saved in `_links.md` |
| `quiz`, `assign` | Description and due-date info captured as Markdown in `_content.md` |

Noise files (logos, templates, Acknowledgement of Country imagery) get routed to a `_misc/` subfolder automatically. You can customize this with `misc_patterns` / `skip_patterns` in config.

Not handled: forum posts, H5P/SCORM content, embedded videos, links inside PDFs.

## Documentation

- **[User Guide](docs/USER_GUIDE.md)** - setup, config, output structure, troubleshooting, FAQ
- **[Developer Guide](docs/DEV_GUIDE.md)** - architecture, handler internals, dedup, contributing
- **[Moodle API Reference](docs/MOODLE_API.md)** - endpoints, fields, failure modes

---

## Technical reference

### Architecture

Everything lives in one file: `course_sync.py`. Key functions:

| Function | What it does |
|---|---|
| `api()` | Moodle REST caller |
| `section_to_week_folder()` | Maps "Week 3 - ..." to `Week3` folder |
| `section_folder_name()` | Maps non-week, non-assessment sections to folder names |
| `lesson_subfolder_from_filename()` | Detects `LessonN/` from filename |
| `is_assessment_section()` | Routes section to `Assessments/` |
| `_is_unit_guide_url()` | Detects unit guide portal URLs |
| `handle_unit_guide()` | Downloads unit guide PDF to course root |
| `handle_contents_module()` | Handles `resource`, `folder`, `page` modules |
| `handle_label_module()` | Extracts files and prose from `label` HTML |
| `handle_url_module()` | Handles `url` modules |
| `html_to_markdown()` | Converts Moodle HTML to Markdown via `markdownify` |
| `download_file()` | Two-tier dedup, writes bytes |
| `sync_course()` | Per-course orchestrator |
| `run_setup()` | Interactive setup wizard |
| `main()` | CLI entry, config loading, course matching |

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
| `output_dir` | Yes | Where course folders are created. Absolute or `~`-prefixed |
| `courses[].code` | Yes | Prefix matched against enrolled course shortname |
| `courses[].folder` | No | Local folder name. Defaults to `code` |
| `log_file` | No | Declared but output currently goes to stdout |
| `misc_patterns` | No | Regex patterns for files to route to `_misc/`. Extends defaults |
| `skip_patterns` | No | Regex patterns for files to skip entirely |

### How deduplication works

Two tiers:

1. **Per-run URL set + path on disk.** If the URL was already hit this run, or the file already exists, it's skipped (`[skip]`).
2. **SHA-256 hash index.** If the file isn't on disk but its content matches something already downloaded, it's logged as `[dup]` and not written again. The index lives at `{output_dir}/.course_sync/downloaded.json` and saves after each course so partial runs don't lose progress.

Dry-run doesn't fetch file bytes, so hash dedup (`[dup]`) won't fire during dry runs.

## License

MIT. See [LICENSE](LICENSE).

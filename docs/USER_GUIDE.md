# User Guide

course-sync is a command-line tool that downloads your course files into clean local folders on your computer. You run it once to get everything, then run it again whenever you want to pick up new uploads. It skips anything already downloaded.

## Contents

1. [What this tool does and doesn't do](#1-what-this-tool-does-and-doesnt-do)
2. [Prerequisites](#2-prerequisites)
3. [Install](#3-install)
4. [Get your Moodle token](#4-get-your-moodle-token)
5. [Set up config.yaml](#5-set-up-configyaml)
6. [Run the sync](#6-run-the-sync)
7. [Output folder structure](#7-output-folder-structure)
8. [Re-syncing](#8-re-syncing)
9. [Troubleshooting](#9-troubleshooting)
10. [FAQ](#10-faq)

---

## 1. What this tool does and doesn't do

**Does:**

- Walk every section of every enrolled course you specify, including General, Study Resources, Textbook recommendations, and all other sections (not just weekly content)
- Download PDFs, slide decks, and other files from `resource`, `folder`, `page`, `label`, and `url` modules
- Save the body of Moodle `page` modules and the chapters of `book` modules as Markdown files (`<page name>.md`, `<book name>.md`) in the section folder
- Sort files into `WeekN/` subfolders based on the Moodle section name
- Sort files into `LessonN/` subfolders based on the filename (e.g. `COMP1000_Lesson3_slides.pdf` goes into `Lesson3/`)
- Route assessment-related sections (containing words like "assessment", "task", "quiz", "exam", "assignment") into an `Assessments/` folder
- Route other named sections (e.g. "General", "Study Resources") into their own sanitized folder names
- Download the unit guide PDF automatically when it detects a link to a unit guide portal
- Capture quiz and assignment descriptions (title, description text, due date info) as readable Markdown in `_content.md`
- Record external non-downloadable links (e.g. YouTube, Echo360) in a `_links.md` file per section
- Skip files already downloaded, so re-runs are fast
- Detect duplicate content across runs using SHA-256 hashing, so the same file uploaded under two different names is only stored once

**Does not:**

- Download forum posts or attachments (these, and any other module type it can't read, print a `[warn]` line and are listed under `Skipped` in that section's `_index.md`, so you can check them on Moodle yourself)
- Download from external video players (Echo360, Panopto, YouTube, etc.)
- Follow hyperlinks inside PDF files
- Handle H5P or SCORM interactive content
- Access anything behind a non-Moodle login page

---

## 2. Prerequisites

Before you start, you need:

- **Python 3.9 or later.** To check: open a terminal and run `python3 --version`. You should see something like `Python 3.11.4`. If you see an error or a version below 3.9, install Python from [python.org](https://www.python.org/downloads/).
- **Git.** To check: run `git --version`. If not installed, download from [git-scm.com](https://git-scm.com/) or install Xcode Command Line Tools on macOS with `xcode-select --install`.
- **A terminal.** On macOS: Terminal or iTerm. On Windows: PowerShell or Windows Terminal. On Linux: any terminal emulator.
- **A Moodle account** at your institution with at least one enrolled course.
- **Web services enabled** on your Moodle instance. Most universities have this on. If yours doesn't, see [FAQ: What if my uni doesn't expose web services?](#what-if-my-uni-doesnt-expose-web-services).

---

## 3. Install

Open your terminal and run these commands one at a time.

**Step 1: Clone the repository**

```
git clone https://github.com/xerlxzx/course-sync course-sync
cd course-sync
```

Expected output (the exact URL and hash will differ):

```
Cloning into 'course-sync'...
remote: Counting objects: 12, done.
```

**Step 2: Install dependencies**

```
pip install -r requirements/course_sync.txt
```

Expected output:

```
Successfully installed PyYAML-6.0.1 requests-2.31.0
```

### Common installation problems

**"pip: command not found" (macOS/Linux)**

Try `pip3` instead:

```
pip3 install -r requirements/course_sync.txt
```

Or use the Python module form:

```
python3 -m pip install -r requirements/course_sync.txt
```

**"Permission denied" (macOS/Linux)**

Do not use `sudo pip`. Instead, install into your user directory:

```
pip3 install --user -r requirements/course_sync.txt
```

**"pip is not recognized" (Windows PowerShell)**

Python's scripts directory may not be on your PATH. Try:

```
python -m pip install -r requirements/course_sync.txt
```

If that fails, reinstall Python from python.org and check "Add Python to PATH" during setup.

**"No module named pip"**

Run:

```
python3 -m ensurepip --upgrade
python3 -m pip install -r requirements/course_sync.txt
```

---

## 4. Get your Moodle token

This tool authenticates using a web service token, which is a long string of letters and numbers tied to your Moodle account. It's not your password.

The steps below work the same at every Moodle-based university. The only difference is the URL you log into. Whether your institution calls it "Moodle", "LMS", or gives it its own name, the token retrieval process is identical because they all run Moodle underneath.

**If you'd rather skip the manual steps, you can run `python3 src/course_sync.py --setup` and the setup wizard will walk you through token entry, config creation, and course selection interactively. That's the easiest path for new users.** Otherwise, follow the steps below.

**Step-by-step:**

1. Log in to your Moodle course platform in a browser, at the URL you normally use to access course materials (for example `https://moodle.example.edu`).

2. Click your **profile photo or initial** in the top-right corner. A menu appears.

3. Click **Profile** in that menu.

4. On your profile page, click **Preferences** (usually near the top or in a sidebar).

5. Under the "User account" section, click **Security keys**.

6. You'll see a table of web service keys. Find the row labelled **Moodle mobile web service**.

7. If the "Key" column is empty, click **Reset**. If a key is already shown, you can click **Reset** to generate a fresh one, or copy the existing key.

8. Copy the full token string. It looks like: `abc123def456ghi789...` (typically 32 characters, all lowercase letters and digits).

9. In the repo root (the `course-sync` directory itself, not `src/`), create a file called `.course_sync_token` and paste the token as the only line:

   ```
   echo "PASTE_YOUR_TOKEN_HERE" > .course_sync_token
   ```

   Replace `PASTE_YOUR_TOKEN_HERE` with your actual token (no quotes needed if using a text editor).

**A few things to keep in mind:**

- **Clicking Reset disconnects the Moodle mobile app.** If you use the official Moodle app on your phone, you'll need to log in again after resetting the key.
- **Some institutions restrict or disable the Security keys page.** If you can't find it, or the "Moodle mobile web service" row is missing, your institution may not allow personal web service tokens. See [FAQ: What if my uni doesn't expose web services?](#what-if-my-uni-doesnt-expose-web-services).
- Keep the token out of version control. The file `.course_sync_token` is already listed in `.gitignore`, so it won't be committed.

**Alternative: environment variable**

If you'd rather not store the token in a file, set it as an environment variable before running:

```
export MOODLE_TOKEN="your_token_here"
python3 src/course_sync.py
```

On Windows PowerShell:

```
$env:MOODLE_TOKEN = "your_token_here"
python3 src/course_sync.py
```

Token resolution order: `--token` flag > `token_file` from config > `MOODLE_TOKEN` environment variable.

---

## 5. Set up config.yaml

**The fastest way to set up your config is to run `python3 src/course_sync.py --setup`.** The wizard asks for your Moodle URL and token, pulls your enrolled courses, and writes the config file for you. If you'd rather do it by hand, read on.

Copy the example config and open it in a text editor:

```
cp config.example.yaml config.yaml
```

The file has four things to configure:

```yaml
moodle:
  base_url: https://moodle.example.edu # your university's Moodle URL, no trailing slash
  token_file: .course_sync_token             # path to your token file

output_dir: ~/Documents/Moodle         # where to put the downloaded files

courses:
  - code: EXAMPLE101                   # prefix of the Moodle course shortname
    folder: EXAMPLE101                 # local folder name under output_dir
```

The `base_url` is your university's Moodle login page URL (without a trailing slash). This is the website where you access your course materials. It might be called "Moodle", "LMS", or something else depending on your institution.

### Field reference

| Field | Required | Description |
|---|---|---|
| `moodle.base_url` | Yes | Base URL of your Moodle site. No trailing slash. |
| `moodle.token_file` | Yes (unless using env var or `--token`) | Path to the file containing your token. Relative paths resolve from the config file's directory. |
| `output_dir` | Yes | Absolute path or `~`-prefixed home-relative path where course folders will be created. |
| `courses[].code` | Yes | Prefix matched against your enrolled course's Moodle shortname. |
| `courses[].folder` | No | Local folder name. Defaults to `code` if omitted. |
| `log_file` | No | Log file path. Defaults to `sync.log` in the repo directory. |
| `misc_patterns` | No | List of regex patterns. Files whose basename matches are routed to `_misc/` instead of the main lesson/section folder. Extends (does not replace) the built-in defaults. |
| `skip_patterns` | No | List of regex patterns. Files whose basename matches are not downloaded at all and are recorded in `Skipped`. |

### How course code matching works

The `code` value is matched as a prefix against each enrolled course's Moodle shortname. For example, `code: COMP1000` will match a course with shortname `COMP1000-S1-2026`. The first config entry whose code is a prefix of an enrolled shortname wins.

If no enrolled courses match any of your configured codes, the tool prints your full list of enrolled courses and exits. You can use that output to find the right prefix.

### Example: student with three units

```yaml
moodle:
  base_url: https://moodle.example.edu
  token_file: .course_sync_token

output_dir: ~/Documents/Courses

courses:
  - code: COMP1000
    folder: COMP1000_Sem1
  - code: COMP2000
    folder: COMP2000
  - code: STAT1000
    folder: STAT1000
```

### Example: student at a different university

```yaml
moodle:
  base_url: https://moodle.myuniversity.edu
  token_file: .course_sync_token

output_dir: /home/alex/uni/courses

courses:
  - code: PHYS101
    folder: PHYS101
  - code: MATH201
    folder: MATH201
```

### Noise routing: `_misc/` and skip patterns

Moodle pages sometimes embed files that aren't actual lesson material: the university logo on every page, the land photo inside "Acknowledgement of Country" labels, assignment templates, and so on. The tool routes these to a `_misc/` subfolder inside each section so the main lesson view stays clean. They're still downloaded (the poster template is useful for assignments), just out of the way.

You don't have to configure anything for this to work. The defaults catch:

- Files named `logo.png` / `logo.jpg` / `logo.svg` / etc.
- Files containing `university_logo` in the name.
- Files matching `poster template`, `academic template`, `presentation template`, `essay template`, `report template`.
- Files literally named `template.pptx`, `template.docx`, etc.
- Any file (image or otherwise) found inside a label whose name contains "acknowledgement".

If you want to extend the defaults with your own patterns, set `misc_patterns` in `config.yaml`. The defaults still apply, and your patterns add to them.

```yaml
misc_patterns:
  - "(?i)welcome.*image"        # any image with "welcome" in the name
  - "(?i)cover[\\s_-]?page"     # cover page PDFs
```

Patterns are Python regex applied to the filename (basename only, after sanitization). Use the inline `(?i)` flag for case-insensitive matching.

If you want some files to be ignored entirely (not even downloaded into `_misc/`), use `skip_patterns`:

```yaml
skip_patterns:
  - "(?i)deprecated"
  - "(?i)old[\\s_-]?version"
```

Skipped files appear in the `Skipped` section of `_index.md` with the reason `matched skip_patterns`, so you can audit what the tool ignored.

---

## 6. Run the sync

**New user? Try `python3 src/course_sync.py --setup` first.** It walks you through everything interactively. You can always switch to manual config later.

**Always do a dry run first.** This shows you what would be downloaded without writing any files:

```
python3 src/course_sync.py --dry-run
```

Expected output:

```
Connecting to Moodle...
Logged in as: Alex Student

============================================================
Course: COMP1000-S1-2026 - Introduction to Computing
Folder: COMP1000_Sem1
============================================================

  Section: Week 1 - Introduction
  [dry]  COMP1000_Sem1/Week1/Lesson1/COMP1000_Wk1_Lesson1.pdf
  [dry]  COMP1000_Sem1/Week1/reading.pdf
  [idx]  COMP1000_Sem1/Week1/_index.md (2 row(s), 0 skipped)
```

When the output looks right, run without `--dry-run`:

```
python3 src/course_sync.py
```

### What each output line means

| Prefix | Meaning |
|---|---|
| `[dl]` | File downloaded and written to disk. |
| `[skip]` | File already exists at that path. Not re-downloaded. For `Unit_Guide.pdf`, `[skip] ... (unchanged)` means the portal's fresh copy only differs in its internal ID and dates, so the file was left alone. |
| `[dup]` | The file's content is already on disk under another path, so it is not written again. The first known location is shown. The first time, the file was fetched and its SHA-256 hash matched the index; afterwards the line ends `recorded, not re-fetched` and nothing is downloaded, as long as the server reports the file unchanged. |
| `[dry]` | Dry-run preview. File would be downloaded, but nothing was written. |
| `[upd]` | The file was replaced on the server under the same name, so the local copy was overwritten. Shows the old and new size. For `Unit_Guide.pdf` it reads `(unit guide changed)`. |
| `[dry-upd]` | Dry-run preview of the same: the server copy changed, nothing was written. |
| `[page]` / `[book]` | A Moodle page or book was saved (or updated) as a Markdown file. |
| `[ok]` | A page or book was checked and the Markdown file on disk is already current. |
| `[dry-page]` / `[dry-book]` | Dry-run preview of a page or book capture. |
| `[locked]` | A book is not available to you yet (time-locked). It is recorded under `Skipped` and captured by the first sync after it opens. |
| `[warn]` | Something needs a look: a module type the tool can't capture, a book with no readable chapters, or a unit guide that couldn't be matched or downloaded. |
| `[fail]` | Something went wrong while fetching or writing it: `[fail] <path or module>: <reason>`, for example a 404, a timeout, a file name the disk refused, a redirect to another site that was refused, or a page or book chapter that could not be fetched. The tool carries on with everything else (the section's `_index.md` is still written and lists the failure under `Skipped`), the rest of the courses are still synced, and at the end it prints `Done, with N failure(s)` and exits with status 1. Run it again: a failed file is retried. The reason never contains your token. |
| `[keep]` | A book had a chapter that failed to download, so the earlier, complete Markdown file was left as it was. Shown together with a `[fail]` line. |
| `[skip-rule]` | A file matched a `skip_patterns` entry and was not downloaded. Recorded in `_index.md` under `Skipped`. |
| `[misc]` | A file matched a noise pattern (default or `misc_patterns`) or came from a label flagged as misc, and was routed to the section's `_misc/` subfolder. Always paired with a `[dl]`, `[dup]`, `[skip]`, or `[dry]` line on the immediately preceding output line for the same path. |
| `[idx]` | The `_index.md` audit file was written (or previewed) for this section. |
| `[links]` | The `_links.md` external links file was written (or previewed) for this section. `[links] ... removed` means the section no longer has any external links, so the `_links.md` the tool wrote earlier was deleted (a file you wrote or edited yourself is never deleted). |
| `[content]` | The `_content.md` rich-text capture file was written (or previewed) for this section. |

About dry-run and dedup: a dry run exits before fetching file bytes, so it cannot discover new duplicates; they show as `[dry]`. Duplicates found by an earlier real run are remembered, and a dry run reports those as `[dup] ... recorded, not re-fetched`.

If two different files in one folder have the same name, or names that differ only by upper/lower case (`Sample output.txt` and `sample output.txt`), the second is saved as `Sample output (2).txt` (then `(3)`, ...) instead of overwriting the first, in the order Moodle lists them. If the second turns out to have identical content it is a `[dup]` and no extra file appears.

### All CLI options

```
python3 src/course_sync.py [--config PATH] [--token TOKEN] [--dry-run] [--setup]
```

| Flag | Default | Notes |
|---|---|---|
| `--config` | `config.yaml` in the repo root | Path to your YAML config file. |
| `--token` | from `token_file` in config, then `MOODLE_TOKEN` env var | Overrides all other token sources. |
| `--dry-run` | off | Preview what would be downloaded. No files written. |
| `--setup` | off | Interactive wizard: walks you through entering your URL, token, picking courses, and generating config. Best for first-time setup. |

**Exit status:** `0` when everything worked, `1` when anything failed (see `[fail]` above) or the tool could not start (bad config, wrong token). If something unexpected goes wrong you get one line starting `Error:` on stderr, never a stack trace and never your token; set `COURSE_SYNC_DEBUG=1` to see a (redacted) traceback.

**Preview both tools at once:** `./sync_all.sh --dry-run` runs `course_sync.py --dry-run`, then `sway_sync.py --check` (which only looks). Nothing is written. A `NOTE` at the end says if Sway found new or changed decks; that is not an error. Any other argument is rejected with a usage message (status 2).

**One module:** `python3 src/sync_one.py <cmid> [--dry-run]` re-fetches a single module (its cmid is the `id=` number in the module's Moodle URL). Exit status: `0` handled, `1` error or unknown cmid, `2` bad command line, `3` nothing written because the module has no file of its own (a quiz or assignment description, label text, an external link, an unsupported type, a locked book: those live in `_content.md` / `_links.md`, which `sync_one.py` never rewrites; run the full sync for them).

---

## 7. Output folder structure

After a sync, your `output_dir` looks like this:

```
output_dir/
├── COMP1000_Sem1/
│   ├── Unit_Guide.pdf          ← auto-downloaded if a unit guide link is found
│   ├── Week1/
│   │   ├── Lesson1/
│   │   │   └── COMP1000_Wk1_Lesson1.pdf
│   │   ├── _misc/
│   │   │   ├── University Logo.png
│   │   │   └── Poster Template.pptx
│   │   ├── COMP1000_Wk1_Reading.pdf
│   │   ├── _content.md
│   │   ├── _index.md
│   │   └── _links.md
│   ├── Week2/
│   │   ├── Lesson1/
│   │   └── _index.md
│   ├── General/                ← non-week, non-assessment sections get their own folder
│   │   └── _index.md
│   └── Assessments/
│       ├── _content.md         ← quiz/assignment info captured here
│       └── _index.md
└── .course_sync/
    └── downloaded.json
```

### Special files

**`_index.md`** is created in each section folder after a sync. It's a Markdown table listing every file the tool found in that section: the source module name, module type, filename, and destination subfolder. It also includes:

- A `Routed to _misc/` table showing files that matched a noise pattern, with the reason (`logo pattern`, `template pattern`, `acknowledgement label`, `matched misc_patterns`).
- An `External links` summary that links to `_links.md` when present.
- A `Skipped` list with reasons (including `matched skip_patterns` for files dropped via config).

It's useful for checking what the tool found versus what you expected, and for confirming why a given file ended up where it did.

The `_index.md` is overwritten on every sync run, so it always reflects the current state of that section.

**`_misc/`** is a subfolder created inside a section when one or more files match the built-in noise patterns or your `misc_patterns`. These are files that are technically part of the course but aren't lecture content (university logos, Acknowledgement of Country imagery, assignment templates, etc.). Keeping them in `_misc/` makes the main lesson view easier to scan. See [section 5: noise routing](#noise-routing-_misc-and-skip-patterns).

**`_links.md`** is created only when a section has external (non-downloadable) links, such as links to lecture recordings on Echo360, or links to external websites posted by lecturers (including files hosted on a different Moodle site: your token is never sent anywhere except your own Moodle). The file contains a Markdown list of those links. Also overwritten on every sync run, and deleted when the section no longer has any links.

**`_content.md`** holds assessment briefs, academic integrity statements, lesson prose, and any other rich text from labels and section summaries, converted to readable Markdown. It's generated automatically and overwritten each sync. Only created when a section has at least one capturable text block (an empty or links-only section won't produce a `_content.md`).

**`.course_sync/downloaded.json`** is a hidden directory and JSON file in your `output_dir`. This is the dedup index: a record of every file ever downloaded, keyed by SHA-256 hash, plus the duplicates it chose not to write. It lets the tool skip re-downloading files even if they're uploaded again under a different name. Don't delete this file unless you want to force a full re-download. If it ever gets corrupted the tool does not throw it away silently: it renames it to `downloaded.json.corrupt-<date>`, prints a warning and starts a new one.

---

## 8. Re-syncing

Re-run the script any time you want to pick up new content:

```
python3 src/course_sync.py
```

On a re-run:

- Files already on disk are printed as `[skip]` and not re-downloaded.
- Files that were downloaded before but are now offered under a different name are detected by SHA-256 hash, printed as `[dup]`, and not re-written. Once recorded, a duplicate is not fetched again.
- Only genuinely new files (different URL and different content) are downloaded and printed as `[dl]`.
- A file that failed last time (`[fail]`) is simply tried again.

Running weekly at the start of each week is a reasonable cadence. If your lecturer uploads files mid-week, just run it whenever you want to catch up.

---

## 9. Troubleshooting

### "Could not authenticate. Check your token."

Your token is wrong, expired, or missing.

- Open `.course_sync_token` and confirm it contains exactly one line with your token, no extra spaces or newlines at the start.
- Log into Moodle, go to Profile > Preferences > Security keys, and reset the Moodle mobile web service token. Copy the new value into `.course_sync_token`.
- If you're using the `MOODLE_TOKEN` environment variable, confirm it's set in the same terminal session where you run the script.

### "No matching courses found"

The `code` values in your config don't match any enrolled course's Moodle shortname. The script prints your full list of enrolled courses when this happens, so use that list to find the correct prefix.

For example, if the list shows `COMP1000-S1-2026` and your config has `code: COMP1000S1`, change it to `code: COMP1000` (a prefix of the actual shortname).

### Section folders are missing or mostly empty

**The section isn't named "Week N" or an assessment keyword.** Sections that don't match the "Week N" pattern or assessment keywords (like "Introduction", "General", "Course Resources") are processed into their own folders based on the section name (e.g. `General/`, `Study_Resources/`). If files are missing, check whether the section appears in the output at all. If the folder exists but is empty, the section may have no downloadable files. Check `_index.md` for what was found.

**The content hasn't been released yet.** Moodle lets lecturers set release dates. If a section or module is hidden or restricted, it won't appear in the API response. Wait until the content is released.

### "Could not connect" or connection errors

- Check that `moodle.base_url` in your config is correct and doesn't have a trailing slash.
- Confirm you can reach the URL in a browser.
- Some institutions restrict web service access to on-campus networks or VPN. If you're off-campus, try connecting via your institution's VPN.

### Files land in the wrong week folder

Files go into the folder corresponding to the Moodle section they're in, not necessarily the week that matches their filename. A file called `Week3_Notes.pdf` that your lecturer placed in the Week 1 section will appear in `Week1/`. Check the `_index.md` in the relevant folder to see which Moodle module and section the file came from.

### Lines starting with `[fail]`, and the run ends with "Done, with N failure(s)"

One or more files or modules could not be fetched or written. The tool carries on with everything else, so most of your material is already in place; each failure is listed under `Skipped` in that section's `_index.md` with the reason. Typical reasons: a 404 for a file the lecturer removed or hid, a timeout (try again), a redirect to another site (the tool refuses to send your token there), or a disk problem. Run the sync again; failed files are retried. The messages never contain your token, so it is safe to paste them into a bug report.

### A file has " (2)" in its name

Two different files in the same folder had the same name, or names that differ only by upper/lower case (macOS treats `Sample output.txt` and `sample output.txt` as one file, so earlier versions kept only one of them). The first one keeps its name and the later ones get `(2)`, `(3)`. The numbers follow the order Moodle lists the files in.

### I can't find a file called index.html

This is expected. Moodle stores the body of every `page` module as an `index.html` file. The tool doesn't save that file as-is; it converts the page to Markdown and saves it as `<page name>.md` in the same section folder (logged as `[page]`). Check the generated file's header: tables and images lose some fidelity in Markdown, so open the page on Moodle if something looks off.

---

## 10. FAQ

### Can I use this with Canvas, Blackboard, or Brightspace?

No. This tool uses Moodle's web services API. Canvas, Blackboard, and Brightspace use different APIs and aren't supported.

### How do I find my Moodle URL?

Your Moodle URL is the website you log into to access your course materials. Different universities brand it differently. It might be called "Moodle", "LMS", "Learning Portal", or something else entirely, but if your university uses Moodle, the URL is what you need for `base_url` in your config.

To find it:

1. Open the website where you normally access your courses in a browser.
2. Look at the address bar. The base URL is everything up to and including the domain name, without any trailing path. For example, if you see `https://moodle.example.edu/my/`, the base URL is `https://moodle.example.edu`.
3. To confirm it's Moodle: scroll to the bottom of the page. Most Moodle sites show "Powered by Moodle" in the footer, or the page source will contain references to Moodle.

Whatever your university calls it, the process is the same. Just use whatever URL you log into.

### Is my password sent anywhere?

No. The tool only uses your web service token, which you retrieve from Moodle's Security keys page. Your password is never sent to or read by this tool.

### What if my uni doesn't expose web services?

The Moodle mobile web service must be enabled by your institution's Moodle administrators. Signs that it's not available:

- The "Security keys" page in your Moodle preferences doesn't exist.
- The "Moodle mobile web service" row is absent from the Security keys table.
- You get a "Service not available" or similar error when the script tries to authenticate.

If that's the case, the tool won't work. You'd need to ask your institution's IT helpdesk whether they can enable the Moodle mobile app web service, or whether there's an alternative.

### How do I add more courses?

Open `config.yaml` and add entries under `courses`:

```yaml
courses:
  - code: COMP1000
    folder: COMP1000
  - code: MATH201
    folder: MATH201
  - code: PHYS101      # add this
    folder: PHYS101    # and this
```

Then run the sync again. Existing downloads aren't affected.

### Can I sync into an iCloud Drive or Dropbox-synced folder?

Yes, but with caveats. Set `output_dir` to a path inside your iCloud Drive or Dropbox folder. The tool writes files normally; the cloud service syncs them.

Potential issues:

- **iCloud Drive on macOS** may evict locally-downloaded files to save space (show as greyed-out with a cloud icon). The tool will re-download them on the next sync if they aren't on disk, which may produce unexpected `[dl]` entries.
- **Dropbox conflicts** can occur if you sync on two devices simultaneously. Avoid running syncs on both devices at the same time.
- If cloud sync is mid-flight when you run the tool, the `.course_sync/downloaded.json` index may be slightly stale. This is harmless. The worst outcome is one file being re-downloaded.

### Why are there random logos or photos in my course folder?

You may see files like `University Logo.png`, other university branding, or an Acknowledgement of Country land photo embedded in your course. The tool routes these into a `_misc/` subfolder inside the relevant section so they're out of the main lesson view but still kept on disk (some, like poster templates, are useful for assignments).

If a file you _want_ to see in the main folder is being misrouted, check the `Routed to _misc/` table in `_index.md` for the reason, and either don't add a custom `misc_patterns` entry that catches it or rename the file upstream in Moodle. If a file you _don't_ want at all is being downloaded, add a `skip_patterns` entry. See [section 5: noise routing](#noise-routing-_misc-and-skip-patterns).

### The tool ran but I cannot find my files

- Check that `output_dir` in your config is correct and that you have write permission to that directory.
- Look at the script's output: `[dl]` lines show the exact path each file was written to, relative to `output_dir`.
- On macOS, if `output_dir` is `~/Documents/Moodle`, the actual path is `/Users/yourname/Documents/Moodle`.

### Why isn't the right sidebar (Studiosity, Leganto, Unit Contacts) in `_content.md`?

The right-hand sidebar blocks you see in Moodle (Studiosity tutoring, Leganto reading lists, Unit Contacts, etc.) are theme blocks served by a different web service than the one course-sync uses. They don't appear in the course content API response, so the tool can't capture them. To access those resources, use the Moodle web UI.

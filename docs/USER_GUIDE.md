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

- Download forum posts or attachments
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
pip install -r requirements.txt
```

Expected output:

```
Successfully installed PyYAML-6.0.1 requests-2.31.0
```

### Common installation problems

**"pip: command not found" (macOS/Linux)**

Try `pip3` instead:

```
pip3 install -r requirements.txt
```

Or use the Python module form:

```
python3 -m pip install -r requirements.txt
```

**"Permission denied" (macOS/Linux)**

Do not use `sudo pip`. Instead, install into your user directory:

```
pip3 install --user -r requirements.txt
```

**"pip is not recognized" (Windows PowerShell)**

Python's scripts directory may not be on your PATH. Try:

```
python -m pip install -r requirements.txt
```

If that fails, reinstall Python from python.org and check "Add Python to PATH" during setup.

**"No module named pip"**

Run:

```
python3 -m ensurepip --upgrade
python3 -m pip install -r requirements.txt
```

---

## 4. Get your Moodle token

This tool authenticates using a web service token, which is a long string of letters and numbers tied to your Moodle account. It's not your password.

The steps below work the same at every Moodle-based university. The only difference is the URL you log into. Whether your institution calls it "iLearn", "Moodle", "LMS", or something else, the token retrieval process is identical because they all run Moodle underneath.

**If you'd rather skip the manual steps, you can run `python3 course_sync.py --setup` and the setup wizard will walk you through token entry, config creation, and course selection interactively. That's the easiest path for new users.** Otherwise, follow the steps below.

**Step-by-step:**

1. Log in to your Moodle course platform in a browser. Examples:
   - Macquarie University: `https://ilearn.mq.edu.au`
   - UNSW: `https://moodle.telt.unsw.edu.au`
   - Monash: `https://learning.monash.edu`
   - Other universities: the URL you normally use to access course materials.

2. Click your **profile photo or initial** in the top-right corner. A menu appears.

3. Click **Profile** in that menu.

4. On your profile page, click **Preferences** (usually near the top or in a sidebar).

5. Under the "User account" section, click **Security keys**.

6. You'll see a table of web service keys. Find the row labelled **Moodle mobile web service**.

7. If the "Key" column is empty, click **Reset**. If a key is already shown, you can click **Reset** to generate a fresh one, or copy the existing key.

8. Copy the full token string. It looks like: `abc123def456ghi789...` (typically 32 characters, all lowercase letters and digits).

9. In the `course-sync` directory, create a file called `.course_sync_token` and paste the token as the only line:

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
python3 course_sync.py
```

On Windows PowerShell:

```
$env:MOODLE_TOKEN = "your_token_here"
python3 course_sync.py
```

Token resolution order: `--token` flag > `token_file` from config > `MOODLE_TOKEN` environment variable.

---

## 5. Set up config.yaml

**The fastest way to set up your config is to run `python3 course_sync.py --setup`.** The wizard asks for your Moodle URL and token, pulls your enrolled courses, and writes the config file for you. If you'd rather do it by hand, read on.

Copy the example config and open it in a text editor:

```
cp config.example.yaml config.yaml
```

The file has four things to configure:

```yaml
moodle:
  base_url: https://ilearn.mq.edu.au   # your university's Moodle URL, no trailing slash
  token_file: .course_sync_token             # path to your token file

output_dir: ~/Documents/Moodle         # where to put the downloaded files

courses:
  - code: EXAMPLE101                   # prefix of the Moodle course shortname
    folder: EXAMPLE101                 # local folder name under output_dir
```

The `base_url` is your university's Moodle login page URL (without a trailing slash). This is the website where you access your course materials. It might be called "iLearn", "Moodle", "LMS", or something else depending on your institution. See `config.example.yaml` for examples from several Australian universities.

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

### Example: Macquarie University student

```yaml
moodle:
  base_url: https://ilearn.mq.edu.au
  token_file: .course_sync_token

output_dir: ~/Documents/Courses

courses:
  - code: COMP1000
    folder: COMP1000_Sem1
  - code: WCOM1300
    folder: WCOM1300
  - code: STAT1250
    folder: STAT1250
```

### Example: UNSW student

```yaml
moodle:
  base_url: https://moodle.telt.unsw.edu.au
  token_file: .course_sync_token

output_dir: ~/Documents/UNSW

courses:
  - code: COMP1511
    folder: COMP1511
  - code: MATH1131
    folder: MATH1131
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

Moodle pages sometimes embed files that aren't actual lesson material: the university logo on every page, the Wallumedegal land photo inside "Acknowledgement of Country" labels, assignment templates, and so on. The tool routes these to a `_misc/` subfolder inside each section so the main lesson view stays clean. They're still downloaded (the poster template is useful for assignments), just out of the way.

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

**New user? Try `python3 course_sync.py --setup` first.** It walks you through everything interactively. You can always switch to manual config later.

**Always do a dry run first.** This shows you what would be downloaded without writing any files:

```
python3 course_sync.py --dry-run
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
python3 course_sync.py
```

### What each output line means

| Prefix | Meaning |
|---|---|
| `[dl]` | File downloaded and written to disk. |
| `[skip]` | File already exists at that path. Not re-downloaded. |
| `[dup]` | File fetched but its SHA-256 hash matches a file already in the index. Content not written again. The first known location is shown. |
| `[dry]` | Dry-run preview. File would be downloaded, but nothing was written. |
| `[skip-html]` | An `index.html` file generated by Moodle's page module was ignored. It is a template, not real content. |
| `[skip-rule]` | A file matched a `skip_patterns` entry and was not downloaded. Recorded in `_index.md` under `Skipped`. |
| `[misc]` | A file matched a noise pattern (default or `misc_patterns`) or came from a label flagged as misc, and was routed to the section's `_misc/` subfolder. Always paired with a `[dl]`, `[dup]`, `[skip]`, or `[dry]` line on the immediately preceding output line for the same path. |
| `[idx]` | The `_index.md` audit file was written (or previewed) for this section. |
| `[links]` | The `_links.md` external links file was written (or previewed) for this section. |
| `[content]` | The `_content.md` rich-text capture file was written (or previewed) for this section. |

About dry-run and dedup: a dry run exits before fetching file bytes, so the SHA-256 dedup check (which produces `[dup]`) doesn't fire during dry runs. The dry-run output shows what paths would be written but can't predict which would be flagged as duplicates.

### All CLI options

```
python3 course_sync.py [--config PATH] [--token TOKEN] [--dry-run] [--setup]
```

| Flag | Default | Notes |
|---|---|---|
| `--config` | `./config.yaml` (next to the script) | Path to your YAML config file. |
| `--token` | from `token_file` in config, then `MOODLE_TOKEN` env var | Overrides all other token sources. |
| `--dry-run` | off | Preview what would be downloaded. No files written. |
| `--setup` | off | Interactive wizard: walks you through entering your URL, token, picking courses, and generating config. Best for first-time setup. |

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
│   │   │   ├── MQ Logo.png
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

**`_links.md`** is created only when a section has external (non-downloadable) links, such as links to lecture recordings on Echo360, or links to external websites posted by lecturers. The file contains a Markdown list of those links. Also overwritten on every sync run.

**`_content.md`** holds assessment briefs, academic integrity statements, lesson prose, and any other rich text from labels and section summaries, converted to readable Markdown. It's generated automatically and overwritten each sync. Only created when a section has at least one capturable text block (an empty or links-only section won't produce a `_content.md`).

**`.course_sync/downloaded.json`** is a hidden directory and JSON file in your `output_dir`. This is the dedup index: a record of every file ever downloaded, keyed by SHA-256 hash. It lets the tool skip re-downloading files even if they're uploaded again under a different name. Don't delete this file unless you want to force a full re-download.

---

## 8. Re-syncing

Re-run the script any time you want to pick up new content:

```
python3 course_sync.py
```

On a re-run:

- Files already on disk are printed as `[skip]` and not re-downloaded.
- Files that were downloaded before but are now offered under a different name are detected by SHA-256 hash, printed as `[dup]`, and not re-written.
- Only genuinely new files (different URL and different content) are downloaded and printed as `[dl]`.

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

### The tool skips a file called index.html

This is expected. Moodle generates an `index.html` template file for every `page` module. It's not real content, so the tool skips it and logs `[skip-html]`.

---

## 10. FAQ

### Can I use this with Canvas, Blackboard, or Brightspace?

No. This tool uses Moodle's web services API. Canvas, Blackboard, and Brightspace use different APIs and aren't supported.

### How do I find my Moodle URL?

Your Moodle URL is the website you log into to access your course materials. Different universities brand it differently. It might be called "iLearn", "Moodle", "LMS", "Learning Portal", or something else entirely, but if your university uses Moodle, the URL is what you need for `base_url` in your config.

To find it:

1. Open the website where you normally access your courses in a browser.
2. Look at the address bar. The base URL is everything up to and including the domain name, without any trailing path. For example, if you see `https://moodle.telt.unsw.edu.au/my/`, the base URL is `https://moodle.telt.unsw.edu.au`.
3. To confirm it's Moodle: scroll to the bottom of the page. Most Moodle sites show "Powered by Moodle" in the footer, or the page source will contain references to Moodle.

Some known Moodle URLs at Australian universities:

- **Macquarie University**: `https://ilearn.mq.edu.au`
- **UNSW**: `https://moodle.telt.unsw.edu.au`
- **Monash University**: `https://learning.monash.edu`

If your university isn't listed, the process is the same. Just use whatever URL you log into.

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

You may see files like `MQ Logo.png`, university branding, or an Acknowledgement of Country land photo embedded in your course. The tool routes these into a `_misc/` subfolder inside the relevant section so they're out of the main lesson view but still kept on disk (some, like poster templates, are useful for assignments).

If a file you _want_ to see in the main folder is being misrouted, check the `Routed to _misc/` table in `_index.md` for the reason, and either don't add a custom `misc_patterns` entry that catches it or rename the file upstream in Moodle. If a file you _don't_ want at all is being downloaded, add a `skip_patterns` entry. See [section 5: noise routing](#noise-routing-_misc-and-skip-patterns).

### The tool ran but I cannot find my files

- Check that `output_dir` in your config is correct and that you have write permission to that directory.
- Look at the script's output: `[dl]` lines show the exact path each file was written to, relative to `output_dir`.
- On macOS, if `output_dir` is `~/Documents/Moodle`, the actual path is `/Users/yourname/Documents/Moodle`.

### Why isn't the right sidebar (Studiosity, Leganto, Unit Contacts) in `_content.md`?

The right-hand sidebar blocks you see in Moodle (Studiosity tutoring, Leganto reading lists, Unit Contacts, etc.) are theme blocks served by a different web service than the one course-sync uses. They don't appear in the course content API response, so the tool can't capture them. To access those resources, use the Moodle web UI.

#!/usr/bin/env python3
"""
moodle-sync (course-sync): downloads and organises files from a Moodle instance.

Usage:
    python3 course_sync.py [--config PATH] [--token TOKEN] [--dry-run]

Config: see config.example.yaml.
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

import requests
import yaml
from markdownify import markdownify as _markdownify


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.yaml"

# Persisted dedup index location, relative to output_dir
INDEX_DIRNAME = ".course_sync"
INDEX_FILENAME = "downloaded.json"

# Subfolder name for files routed away from the main lesson/section view.
MISC_SUBFOLDER = "_misc"

# Default filename patterns that route to _misc/. Matched against the
# basename (post-sanitization), case-insensitive via inline (?i) flags
# in each pattern. Extended (never replaced) by config.misc_patterns.
DEFAULT_MISC_PATTERNS: List[str] = [
    # University branding (logos)
    r"(?i)\blogo\.(png|jpe?g|svg|gif|webp)$",
    r"(?i)\buniversity[\s_-]*logo",
    # Templates (assignment / poster / document templates)
    r"(?i)\b(poster|academic|presentation|essay|report)[\s_-]*template",
    r"(?i)template\.(pptx?|docx?|xlsx?)$",
]

# Substring (case-insensitive) of a label module's name that flags every
# file extracted from that label as misc. Acknowledgement-of-Country
# labels embed land photos that aren't lesson material.
MISC_LABEL_NAME_SUBSTRINGS: List[str] = ["acknowledgement"]


def api(base_url: str, token: str, function: str, **params):
    url = f"{base_url}/webservice/rest/server.php"
    r = requests.get(url, params={
        "wstoken": token,
        "wsfunction": function,
        "moodlewsrestformat": "json",
        **params,
    })
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "exception" in data:
        raise RuntimeError(f"Moodle API error: {data.get('message', data)}")
    return data


def get_user_id(base_url: str, token: str):
    info = api(base_url, token, "core_webservice_get_site_info")
    return info["userid"], info["fullname"], info


def parse_moodle_version(site_info: dict) -> Tuple[Optional[str], Optional[int]]:
    """
    Extract a human-readable Moodle version string and the raw build number
    from the site info dict returned by ``core_webservice_get_site_info``.

    Returns ``(display_string, build_number)`` where:
    - ``display_string`` is e.g. ``"4.4 (2024-04)"`` or ``None`` if neither
      ``release`` nor ``version`` are usable.
    - ``build_number`` is the integer form of ``version`` (e.g. 2024042200)
      or ``None`` if parsing fails.
    """
    release_raw = str(site_info.get("release", "")).strip()
    version_raw = str(site_info.get("version", "")).strip()

    # Parse the short release label (e.g. "4.4" from "4.4+ (Build: 20240809)")
    release_short: Optional[str] = None
    if release_raw:
        m = re.match(r"^(\d+\.\d+)", release_raw)
        if m:
            release_short = m.group(1)

    # Parse build number and date from the version integer string
    build_number: Optional[int] = None
    date_suffix: Optional[str] = None
    if version_raw and len(version_raw) >= 6:
        try:
            build_number = int(version_raw)
            year = version_raw[:4]
            month = version_raw[4:6]
            date_suffix = f"{year}-{month}"
        except ValueError:
            pass

    # Assemble display string
    if release_short and date_suffix:
        display = f"{release_short} ({date_suffix})"
    elif release_short:
        display = release_short
    elif date_suffix:
        display = f"build {version_raw} ({date_suffix})"
    else:
        display = None

    return display, build_number


# Moodle 3.9 was released as build 2020061500.
MOODLE_39_BUILD = 2020061500


def get_courses(base_url: str, token: str, user_id):
    return api(base_url, token, "core_enrol_get_users_courses", userid=user_id)


def get_contents(base_url: str, token: str, course_id):
    return api(base_url, token, "core_course_get_contents", courseid=course_id)


def section_to_week_folder(name: str) -> Optional[str]:
    """Map a Moodle section name to a local week folder name."""
    m = re.search(r"[Ww]eek\s*(\d+)", name)
    if not m:
        return None
    num = m.group(1)
    folder = f"Week{num}"
    if re.search(r"study", name, re.IGNORECASE):
        folder += "_StudyWeek"
    return folder


def lesson_subfolder_from_filename(filename: str) -> Optional[str]:
    """Detect a Lesson subfolder from the filename only."""
    m = re.search(r"[Ll]esson\s*(\d+)", filename)
    if m:
        return f"Lesson{m.group(1)}"
    return None


def is_assessment_section(name: str) -> bool:
    return bool(re.search(r"assess|task|quiz|exam|assignment", name, re.IGNORECASE))


def section_folder_name(sec_name: str, section_index: int) -> str:
    """Generate a folder name for a non-week, non-assessment section."""
    clean = re.sub(r'<[^>]+>', '', sec_name).strip()
    if not clean:
        return "_General" if section_index == 0 else f"_Section{section_index}"
    clean = sanitize_filename(clean)
    return clean.replace(' ', '_')


def sanitize_filename(name: str) -> str:
    """
    Strip cross-platform-unsafe characters from a filename.

    Removes: : ? * | < > \\ /
    Plus leading/trailing whitespace and trailing dots.
    Collapses internal whitespace to single spaces.
    Always returns a non-empty string ("untitled" as fallback).
    """
    if not name:
        return "untitled"
    cleaned = re.sub(r'[:?*|<>\\/]', '', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = cleaned.rstrip('.')
    cleaned = cleaned.strip()
    return cleaned if cleaned else "untitled"


def collapse_ws(s: str) -> str:
    """Collapse all whitespace runs in a string to single spaces and trim."""
    return re.sub(r'\s+', ' ', s).strip()


def clean_section_name(name: str) -> str:
    """
    Strip HTML tags, decode entities, and trim a Moodle section name.

    Moodle returns section names with HTML-encoded ampersands and similar
    entities (e.g. ``Week 1: Cyber Security &amp; Hygiene``), and
    occasionally with inline HTML (e.g. ``<b></b>``). This helper
    cleans them so they render correctly in ``_content.md``, ``_index.md``,
    and console log lines.
    """
    cleaned = re.sub(r'<[^>]+>', '', name or '')
    return html.unescape(cleaned).strip()


def short_course_code(shortname: str) -> str:
    """
    Reduce a Moodle course shortname to its bare course code.

    Moodle shortnames at Macquarie look like ``WSTA1250_MQUC2_2026_ALL_U``.
    For headings in ``_content.md`` we want just ``WSTA1250``. The heuristic
    keeps the leading run of letters followed by digits and drops everything
    after; if the shortname doesn't follow that pattern, it is returned
    unchanged.
    """
    if not shortname:
        return ""
    s = shortname.strip()
    m = re.match(r"^([A-Za-z]+\d+)", s)
    if m:
        return m.group(1)
    return s


# ---------------------------------------------------------------------------
# HTML -> Markdown conversion (label and section-summary prose capture)
# ---------------------------------------------------------------------------

def html_to_markdown(html_str: str) -> str:
    """
    Convert a Moodle HTML fragment (label description / section summary) to
    readable Markdown using ``markdownify``.

    Strips the ``<div class="no-overflow">`` wrapper Moodle adds around
    every rich-text fragment, then runs markdownify with ATX-style headings
    and ``-`` bullets to match the rest of our generated Markdown. Triple
    blank lines are collapsed to double so output stays compact.
    """
    if not html_str or not html_str.strip():
        return ""
    cleaned = re.sub(r'<div class="no-overflow">', '', html_str, count=1)
    cleaned = re.sub(r'</div>\s*$', '', cleaned, count=1)
    md = _markdownify(cleaned, heading_style="ATX", bullets="-")
    md = re.sub(r'\n{3,}', '\n\n', md)
    return md.strip()


def extract_heading_from_html(html_str: str, fallback_name: str) -> str:
    """
    Return the first ``<h2>``/``<h3>``/``<h4>`` text from ``html_str``, or a
    truncated form of ``fallback_name`` if no heading is found.

    Many Moodle labels store a clean human-readable heading inside the
    description body even though their ``name`` field is the auto-generated
    UPPERCASE-truncated-prose version. Preferring the embedded heading
    yields much nicer ``_content.md`` titles.
    """
    m = re.search(
        r'<h[234][^>]*>(.*?)</h[234]>',
        html_str or "",
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        text = re.sub(r'<[^>]+>', '', m.group(1))
        text = html.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            return text
    name = (fallback_name or "").strip()
    if len(name) > 60:
        name = name[:60].rstrip() + "..."
    return name or "(untitled)"


def is_content_block_skippable(markdown_body: str) -> bool:
    """
    Decide whether a content block should be omitted from ``_content.md``.

    Skip when the body adds no real information:

    1. Empty or whitespace-only after conversion.
    2. Only links with no other prose.
    3. A single heading line (possibly preceded by nav links). Section
       dividers like ``[back to top](#top)\\n\\n### Lesson 1`` fall here.
    4. Fewer than 20 non-link characters. "In Class" labels and similar
       one-phrase dividers that carry no prose content.
    """
    if not markdown_body or not markdown_body.strip():
        return True
    # Strip Markdown link syntax to detect link-only / nav-only content.
    no_links = re.sub(r'\[([^\]]*)\]\([^)]*\)', '', markdown_body).strip()
    if not no_links:
        return True
    # A single heading line remaining (no further body text) is a divider.
    if re.match(r'^#+\s+\S[^\n]*$', no_links):
        return True
    # Trivially short content even after removing links. Not real prose.
    if len(no_links) < 20:
        return True
    return False


def make_download_url(url: str, token: str) -> str:
    """Convert a pluginfile URL to the webservice download form and append token."""
    if "/webservice/pluginfile.php/" not in url:
        dl_url = url.replace("/pluginfile.php/", "/webservice/pluginfile.php/")
    else:
        dl_url = url
    if "token=" not in dl_url:
        sep = "&" if "?" in dl_url else "?"
        dl_url = f"{dl_url}{sep}token={token}"
    return dl_url


def _rel_for_display(path: Path, output_dir: Path) -> str:
    """Return a path string relative to output_dir if possible, else absolute."""
    try:
        return str(path.relative_to(output_dir))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Noise routing: misc patterns and skip patterns
# ---------------------------------------------------------------------------

def _match_pattern(filename: str, patterns: List[str]) -> Optional[str]:
    """
    Return the first pattern in `patterns` that matches `filename` (via
    re.search), or None if nothing matches. Invalid regex patterns are
    skipped silently. A bad user pattern should not break the sync.
    """
    if not filename:
        return None
    for pat in patterns:
        try:
            if re.search(pat, filename):
                return pat
        except re.error:
            continue
    return None


def is_misc_file(filename: str, extra_patterns: List[str]) -> Optional[str]:
    """
    Decide whether `filename` (basename) should route to _misc/.

    Returns a short human-readable reason string (used in log lines and
    _index.md) if any pattern matches, else None. The check runs the
    built-in DEFAULT_MISC_PATTERNS first, then any extra patterns from
    user config (which extend, not replace, the defaults).
    """
    if _match_pattern(filename, DEFAULT_MISC_PATTERNS) is not None:
        return _classify_misc_reason(filename)
    if _match_pattern(filename, extra_patterns) is not None:
        return "matched misc_patterns"
    return None


def _classify_misc_reason(filename: str) -> str:
    """Short human-readable reason for a default-misc match."""
    lower = filename.lower()
    if "logo" in lower:
        return "logo pattern"
    if "template" in lower:
        return "template pattern"
    return "default misc pattern"


def should_skip_file(filename: str, skip_patterns: List[str]) -> bool:
    """Return True if `filename` matches any skip pattern from config."""
    if not skip_patterns:
        return False
    return _match_pattern(filename, skip_patterns) is not None


def label_is_misc(mod_name: str) -> bool:
    """
    Return True if a label module's name marks all its content as misc
    (e.g. "Acknowledgement of Country" labels embedding land imagery).
    """
    if not mod_name:
        return False
    lower = mod_name.lower()
    return any(s in lower for s in MISC_LABEL_NAME_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Persisted hash-dedup index
# ---------------------------------------------------------------------------

def load_hash_index(output_dir: Path) -> dict:
    """Load the cross-run hash index from <output_dir>/.course_sync/downloaded.json."""
    index_path = output_dir / INDEX_DIRNAME / INDEX_FILENAME
    if not index_path.exists():
        return {"files": {}}
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "files" not in data:
            return {"files": {}}
        return data
    except (json.JSONDecodeError, OSError):
        return {"files": {}}


def save_hash_index(output_dir: Path, index: dict) -> None:
    """Atomically write the hash index to <output_dir>/.course_sync/downloaded.json."""
    index_dir = output_dir / INDEX_DIRNAME
    index_dir.mkdir(parents=True, exist_ok=True)
    final_path = index_dir / INDEX_FILENAME
    tmp_path = index_dir / (INDEX_FILENAME + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, sort_keys=True)
    os.replace(tmp_path, final_path)


def record_hash_path(index: dict, sha256: str, size: int, rel_path: str) -> None:
    """Record (or extend) an entry in the hash index. rel_path is relative to output_dir."""
    files = index.setdefault("files", {})
    entry = files.get(sha256)
    if entry is None:
        files[sha256] = {
            "size": size,
            "paths": [rel_path],
            "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    else:
        if rel_path not in entry.get("paths", []):
            entry.setdefault("paths", []).append(rel_path)


# ---------------------------------------------------------------------------
# File download with two-tier dedup
# ---------------------------------------------------------------------------

def download_file(
    token: str,
    fileurl: str,
    filename: str,
    dest: Path,
    dry_run: bool,
    downloaded_urls: Set[str],
    output_dir: Path,
    hash_index: dict,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Download a file with two-tier dedup. Returns (outcome, dest_rel) where:

      outcome is one of:
        "downloaded": bytes fetched and written to disk
        "skipped":    already known via per-run URL set or already on disk
        "dup":        fetched and hashed, but content already in index (not written)
        "dry":        dry-run preview only
        None:         could not proceed (e.g., empty filename)

    dest_rel is a display-friendly path string relative to output_dir.
    """
    if not filename:
        return None, None

    # Tier 1a: per-run URL set
    if fileurl in downloaded_urls:
        return "skipped", None
    downloaded_urls.add(fileurl)

    dest_path = dest / filename
    dest_rel = _rel_for_display(dest_path, output_dir)

    # Tier 1b: same path already on disk
    if dest_path.exists():
        print(f"  [skip] {dest_rel}")
        return "skipped", dest_rel

    if dry_run:
        print(f"  [dry]  {dest_rel}")
        return "dry", dest_rel

    dl_url = make_download_url(fileurl, token)

    # Fetch bytes into memory so we can hash before writing
    r = requests.get(dl_url, timeout=120)
    r.raise_for_status()
    body = r.content
    sha256 = hashlib.sha256(body).hexdigest()
    size = len(body)

    # Tier 2: cross-run hash dedup
    existing = hash_index.get("files", {}).get(sha256)
    if existing is not None:
        existing_paths = existing.get("paths", [])
        present_paths = [p for p in existing_paths if (output_dir / p).exists()]
        if present_paths:
            print(f"  [dup]  {dest_rel}  (same content as {present_paths[0]})")
            record_hash_path(hash_index, sha256, size, dest_rel)
            return "dup", dest_rel
        # Every previously recorded copy is gone from disk (e.g. deleted by the
        # user) - the index entry is stale, so fall through and write the file.

    # Write the file
    print(f"  [dl]   {dest_rel}")
    dest.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(body)
    record_hash_path(hash_index, sha256, size, dest_rel)
    return "downloaded", dest_rel


# ---------------------------------------------------------------------------
# HTML link extraction
# ---------------------------------------------------------------------------

def _strip_tags(s: str) -> str:
    """Strip HTML tags and collapse whitespace. Tags are replaced with a space
    so that text on either side of a <br> or block tag stays separated."""
    no_tags = re.sub(r'<[^>]+>', ' ', s)
    no_tags = html.unescape(no_tags)
    return collapse_ws(no_tags)


def extract_links_from_html(description: str) -> List[Tuple[str, str]]:
    """
    Return a list of (url, display_text) tuples from an HTML string.

    For <a href="X">INNER</a> tags, display_text is the stripped inner text
    (tags removed, whitespace collapsed). For <img src="X"> tags with no
    surrounding anchor, display_text is the filename from the URL path, or
    "image" if not derivable.

    Fragments (#...) and mailto: links are excluded.
    """
    results: List[Tuple[str, str]] = []
    seen_positions: List[Tuple[int, int]] = []

    # Anchor tags: <a ... href="URL" ...>INNER</a>
    anchor_re = re.compile(
        r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for m in anchor_re.finditer(description):
        href = html.unescape(m.group(1)).strip()
        if not href or href.startswith("#") or href.lower().startswith("mailto:"):
            continue
        inner = _strip_tags(m.group(2))
        if not inner:
            inner = href
        results.append((href, inner))
        seen_positions.append((m.start(), m.end()))

    # <img src="URL"> tags, but skip any that fall inside an already-captured anchor
    img_re = re.compile(
        r'<img\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    for m in img_re.finditer(description):
        pos = m.start()
        inside_anchor = any(s <= pos < e for s, e in seen_positions)
        if inside_anchor:
            continue
        src = html.unescape(m.group(1)).strip()
        if not src or src.startswith("#") or src.lower().startswith("mailto:"):
            continue
        # Derive a display name from the URL path
        path_tail = unquote(Path(urlparse(src).path).name)
        display = path_tail if path_tail else "image"
        results.append((src, display))

    return results


# ---------------------------------------------------------------------------
# Module handlers
# ---------------------------------------------------------------------------

def _row(
    rows: List[Tuple[str, str, str, str]],
    mod_name: str,
    modname: str,
    filename: str,
    dest_rel_section: str,
) -> None:
    """Append a row to the section's _index.md table."""
    rows.append((collapse_ws(mod_name) or "(unnamed)", modname, filename, dest_rel_section))


def _dest_label(dest: Path, section_dir: Path) -> str:
    """Format the destination column for _index.md: 'Lesson1/' or '(root)'."""
    if dest == section_dir:
        return "(root)"
    try:
        rel = dest.relative_to(section_dir)
    except ValueError:
        return str(dest)
    rel_str = str(rel)
    if not rel_str or rel_str == ".":
        return "(root)"
    return rel_str + "/"


def handle_contents_module(
    token: str,
    mod: dict,
    section_dir: Path,
    dry_run: bool,
    downloaded_urls: Set[str],
    output_dir: Path,
    hash_index: dict,
    section_state: dict,
    misc_patterns: List[str],
    skip_patterns: List[str],
) -> int:
    """
    Handle modname in (resource, folder, page): iterate contents[] file entries.
    Returns count of files downloaded (excludes dup/skip/dry).
    """
    count = 0
    modname = mod.get("modname", "")
    mod_name = mod.get("name", "").strip()
    contents = mod.get("contents", [])
    file_contents = [c for c in contents if c.get("type") == "file"]

    for fc in file_contents:
        fileurl = fc.get("fileurl", "")
        if not fileurl:
            continue
        raw_filename = fc.get("filename") or Path(urlparse(fileurl).path).name
        filename = sanitize_filename(unquote(raw_filename))

        # Skip Moodle's auto-generated page templates
        if modname == "page" and filename.lower() == "index.html":
            section_state["skipped"].append("index.html (Moodle page template, not content)")
            print(f"  [skip-html] {filename} (page template)")
            continue

        # Config-driven hard skip: never download, never record on disk.
        if should_skip_file(filename, skip_patterns):
            section_state["skipped"].append(f"{filename} (matched skip_patterns)")
            print(f"  [skip-rule] {filename} (matched skip_patterns)")
            continue

        misc_reason = is_misc_file(filename, misc_patterns)
        if misc_reason:
            dest = section_dir / MISC_SUBFOLDER
        else:
            lesson = lesson_subfolder_from_filename(filename)
            dest = section_dir / lesson if lesson else section_dir

        outcome, _dest_rel = download_file(
            token, fileurl, filename, dest, dry_run, downloaded_urls, output_dir, hash_index
        )
        if outcome is None:
            continue
        if outcome == "downloaded":
            count += 1
        if misc_reason:
            print(f"  [misc]  {_dest_rel}  (noise: {misc_reason})")
            section_state["misc_rows"].append(
                (collapse_ws(mod_name) or "(unnamed)", modname, filename, misc_reason)
            )
        _row(section_state["rows"], mod_name, modname, filename, _dest_label(dest, section_dir))

    return count


def handle_label_module(
    token: str,
    mod: dict,
    section_dir: Path,
    dry_run: bool,
    downloaded_urls: Set[str],
    output_dir: Path,
    hash_index: dict,
    section_state: dict,
    misc_patterns: List[str],
    skip_patterns: List[str],
) -> int:
    """
    Handle modname=label: parse description HTML for href/src links.
    Pluginfile links are downloaded; others are appended to external_links as
    (display_text, url) tuples.
    Returns count of files downloaded.
    """
    count = 0
    description = mod.get("description", "")
    mod_name = mod.get("name", "").strip()
    external_links = section_state["external_links"]

    # Compute once per label. Every file from this label inherits the flag.
    is_misc_label = label_is_misc(mod_name)

    # Capture the label's prose as a content block, unless this is a misc
    # label (Acknowledgement of Country etc.) whose body is boilerplate.
    if not is_misc_label:
        md_body = html_to_markdown(description)
        if not is_content_block_skippable(md_body):
            heading = extract_heading_from_html(description, mod_name)
            section_state["content_blocks"].append((heading, "label", md_body))

    for link, display in extract_links_from_html(description):
        if "pluginfile.php" in link:
            raw_filename = unquote(Path(urlparse(link).path).name)
            filename = sanitize_filename(raw_filename)
            if not filename:
                continue

            if should_skip_file(filename, skip_patterns):
                section_state["skipped"].append(f"{filename} (matched skip_patterns)")
                print(f"  [skip-rule] {filename} (matched skip_patterns)")
                continue

            pattern_reason = is_misc_file(filename, misc_patterns)
            if is_misc_label:
                misc_reason: Optional[str] = "acknowledgement label"
            else:
                misc_reason = pattern_reason

            if misc_reason:
                dest = section_dir / MISC_SUBFOLDER
            else:
                lesson = lesson_subfolder_from_filename(filename)
                dest = section_dir / lesson if lesson else section_dir

            outcome, _dest_rel = download_file(
                token, link, filename, dest, dry_run, downloaded_urls, output_dir, hash_index
            )
            if outcome is None:
                continue
            if outcome == "downloaded":
                count += 1
            if misc_reason:
                print(f"  [misc]  {_dest_rel}  (noise: {misc_reason})")
                section_state["misc_rows"].append(
                    (collapse_ws(mod_name) or "(unnamed)", "label", filename, misc_reason)
                )
            _row(section_state["rows"], mod_name, "label", filename, _dest_label(dest, section_dir))
        else:
            if link.startswith("http"):
                external_links.append((display, link))

    return count


def handle_url_module(
    token: str,
    mod: dict,
    section_dir: Path,
    dry_run: bool,
    downloaded_urls: Set[str],
    output_dir: Path,
    hash_index: dict,
    section_state: dict,
    misc_patterns: List[str],
    skip_patterns: List[str],
) -> int:
    """
    Handle modname=url: if the URL points to a pluginfile, download it.
    Otherwise record it as an external link.
    """
    contents = mod.get("contents", [])
    if not contents:
        return 0

    fileurl = contents[0].get("fileurl", "")
    if not fileurl:
        return 0

    mod_name = mod.get("name", "").strip()

    if "pluginfile.php" in fileurl:
        raw_filename = unquote(Path(urlparse(fileurl).path).name)
        filename = sanitize_filename(raw_filename)
        if not filename:
            return 0

        if should_skip_file(filename, skip_patterns):
            section_state["skipped"].append(f"{filename} (matched skip_patterns)")
            print(f"  [skip-rule] {filename} (matched skip_patterns)")
            return 0

        misc_reason = is_misc_file(filename, misc_patterns)
        if misc_reason:
            dest = section_dir / MISC_SUBFOLDER
        else:
            lesson = lesson_subfolder_from_filename(filename)
            dest = section_dir / lesson if lesson else section_dir

        outcome, _dest_rel = download_file(
            token, fileurl, filename, dest, dry_run, downloaded_urls, output_dir, hash_index
        )
        if outcome is None:
            return 0
        if misc_reason:
            print(f"  [misc]  {_dest_rel}  (noise: {misc_reason})")
            section_state["misc_rows"].append(
                (collapse_ws(mod_name) or "(unnamed)", "url", filename, misc_reason)
            )
        _row(section_state["rows"], mod_name, "url", filename, _dest_label(dest, section_dir))
        return 1 if outcome == "downloaded" else 0

    if fileurl.startswith("http"):
        display = collapse_ws(mod_name) or fileurl
        section_state["external_links"].append((display, fileurl))
    return 0


# ---------------------------------------------------------------------------
# Unit guide PDF download
# ---------------------------------------------------------------------------

def _is_unit_guide_url(url: str) -> bool:
    """Check if a URL points to a unit guide portal (e.g. unitguides.mq.edu.au)."""
    try:
        host = urlparse(url).hostname or ""
        return "unitguide" in host.lower()
    except Exception:
        return False


def handle_unit_guide(
    fileurl: str,
    course_dir: Path,
    dry_run: bool,
    output_dir: Path,
    hash_index: dict,
    downloaded_urls: Set[str],
) -> bool:
    """
    Download a unit guide PDF from a university unit-guide portal.

    Follows the search/redirect page to locate the offering ID, then
    downloads the printer-friendly PDF into the course root folder.
    Returns True if a file was downloaded.
    """
    if fileurl in downloaded_urls:
        return False
    downloaded_urls.add(fileurl)

    dest_path = course_dir / "Unit_Guide.pdf"
    dest_rel = _rel_for_display(dest_path, output_dir)

    if dest_path.exists():
        print(f"  [skip] {dest_rel}")
        return False

    if dry_run:
        print(f"  [dry]  {dest_rel}")
        return False

    try:
        r = requests.get(fileurl, timeout=30, allow_redirects=True)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] Could not fetch unit guide page: {e}")
        return False

    offering_match = re.search(r'/unit_offerings/(\d+)', r.text)
    if not offering_match:
        offering_match = re.search(r'/unit_offerings/(\d+)', r.url)
    if not offering_match:
        print(f"  [warn] Could not find unit offering ID at {fileurl}")
        return False

    offering_id = offering_match.group(1)
    parsed = urlparse(r.url)
    pdf_url = f"{parsed.scheme}://{parsed.hostname}/unit_offerings/{offering_id}/unit_guide/print.pdf"

    try:
        r = requests.get(pdf_url, timeout=120)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] Could not download unit guide PDF: {e}")
        return False

    content_type = r.headers.get("Content-Type", "")
    if "pdf" not in content_type.lower():
        print(f"  [warn] Unit guide response was not a PDF (got {content_type})")
        return False

    body = r.content
    sha256 = hashlib.sha256(body).hexdigest()
    size = len(body)

    existing = hash_index.get("files", {}).get(sha256)
    if existing is not None:
        first_path = existing.get("paths", ["?"])[0]
        print(f"  [dup]  {dest_rel}  (same content as {first_path})")
        record_hash_path(hash_index, sha256, size, dest_rel)
        return False

    print(f"  [dl]   {dest_rel}")
    course_dir.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(body)
    record_hash_path(hash_index, sha256, size, dest_rel)
    return True


# ---------------------------------------------------------------------------
# Section-level output: _links.md and _index.md
# ---------------------------------------------------------------------------

def write_links_file(
    section_dir: Path,
    week_label: str,
    external_links: List[Tuple[str, str]],
    dry_run: bool,
    output_dir: Path,
):
    """Write external links collected for a section to _links.md."""
    if not external_links:
        return

    links_path = section_dir / "_links.md"

    lines = [f"# External links - {week_label}", ""]
    for name, url in external_links:
        display = name if name else url
        lines.append(f"- [{display}]({url})")
    lines.append("")
    content = "\n".join(lines)

    if dry_run:
        print(f"  [dry]  {_rel_for_display(links_path, output_dir)} ({len(external_links)} external link(s))")
        return

    section_dir.mkdir(parents=True, exist_ok=True)
    links_path.write_text(content, encoding="utf-8")
    print(f"  [links] {_rel_for_display(links_path, output_dir)} ({len(external_links)} external link(s))")


def write_section_content(
    section_dir: Path,
    section_display_name: str,
    course_code: str,
    content_blocks: List[Tuple[str, str, str]],
    dry_run: bool,
    output_dir: Path,
) -> bool:
    """
    Write the per-section ``_content.md`` rich-text capture file.

    Returns True if the file was (or would be) written, False otherwise.
    Each block is rendered with a ``##`` heading, separated by ``---``
    horizontal rules. The top-level ``#`` heading uses the cleaned section
    name and the short course code, e.g. ``# Assessments - WSTA1250``.
    """
    if not content_blocks:
        return False

    content_path = section_dir / "_content.md"
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M")

    course_suffix = f" - {course_code}" if course_code else ""
    lines: List[str] = [f"# {section_display_name}{course_suffix}", ""]
    lines.append(f"_Last sync: {now_local}_")
    lines.append("")

    rendered_blocks: List[str] = []
    for heading, _source, body in content_blocks:
        block_lines = [f"## {heading}", "", body.strip()]
        rendered_blocks.append("\n".join(block_lines))
    lines.append("\n\n---\n\n".join(rendered_blocks))
    lines.append("")
    content = "\n".join(lines)

    block_count = f"{len(content_blocks)} block(s)"

    if dry_run:
        print(f"  [content] {_rel_for_display(content_path, output_dir)} ({block_count})")
        return True

    section_dir.mkdir(parents=True, exist_ok=True)
    content_path.write_text(content, encoding="utf-8")
    print(f"  [content] {_rel_for_display(content_path, output_dir)} ({block_count})")
    return True


def write_section_index(
    section_dir: Path,
    week_label: str,
    section_state: dict,
    dry_run: bool,
    output_dir: Path,
    has_content_md: bool = False,
):
    """Write the per-section _index.md audit file."""
    rows: List[Tuple[str, str, str, str]] = section_state.get("rows", [])
    misc_rows: List[Tuple[str, str, str, str]] = section_state.get("misc_rows", [])
    external_links: List[Tuple[str, str]] = section_state.get("external_links", [])
    skipped: List[str] = section_state.get("skipped", [])
    content_blocks: List[Tuple[str, str, str]] = section_state.get("content_blocks", [])

    if not rows and not misc_rows and not external_links and not skipped and not has_content_md:
        return

    index_path = section_dir / "_index.md"

    def esc(s: str) -> str:
        # Escape pipe characters that would break the markdown table.
        return s.replace("|", "\\|")

    now_local = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: List[str] = []
    lines.append(f"# {week_label} - auto-generated by course-sync")
    lines.append("")
    lines.append(f"_Last sync: {now_local}_")
    lines.append("")
    lines.append("## Files")
    lines.append("")
    if rows:
        lines.append("| Source module | Type | File | Destination |")
        lines.append("|---|---|---|---|")
        for src, mtype, fname, dest in rows:
            lines.append(f"| {esc(src)} | {esc(mtype)} | {esc(fname)} | {esc(dest)} |")
    else:
        lines.append("_None._")
    lines.append("")
    lines.append("## Routed to _misc/")
    lines.append("")
    if misc_rows:
        lines.append("| Source module | Type | File | Reason |")
        lines.append("|---|---|---|---|")
        for src, mtype, fname, reason in misc_rows:
            lines.append(f"| {esc(src)} | {esc(mtype)} | {esc(fname)} | {esc(reason)} |")
    else:
        lines.append("_None._")
    lines.append("")
    lines.append("## External links")
    lines.append("")
    if external_links:
        lines.append("See `_links.md`.")
    else:
        lines.append("_None._")
    lines.append("")
    lines.append("## Skipped")
    lines.append("")
    if skipped:
        for item in skipped:
            lines.append(f"- {item}")
    else:
        lines.append("_None._")
    lines.append("")
    if has_content_md:
        lines.append("## Prose content")
        lines.append("")
        lines.append(
            f"See `_content.md` ({len(content_blocks)} block(s) "
            "captured from labels + section summary)."
        )
        lines.append("")
    content = "\n".join(lines)

    summary_parts = [
        f"{len(rows)} row(s)",
        f"{len(misc_rows)} misc",
        f"{len(skipped)} skipped",
    ]
    if has_content_md:
        summary_parts.append(f"{len(content_blocks)} content block(s)")
    summary = "(" + ", ".join(summary_parts) + ")"

    if dry_run:
        print(f"  [idx]  {_rel_for_display(index_path, output_dir)} {summary}")
        return

    section_dir.mkdir(parents=True, exist_ok=True)
    index_path.write_text(content, encoding="utf-8")
    print(f"  [idx]  {_rel_for_display(index_path, output_dir)} {summary}")


# ---------------------------------------------------------------------------
# Course sync orchestration
# ---------------------------------------------------------------------------

def sync_course(
    base_url: str,
    token: str,
    course: dict,
    course_dir: Path,
    dry_run: bool,
    output_dir: Path,
    hash_index: dict,
    misc_patterns: List[str],
    skip_patterns: List[str],
):
    print(f"\n{'='*60}")
    print(f"Course: {course['shortname']} - {course['fullname']}")
    print(f"Folder: {_rel_for_display(course_dir, output_dir)}")
    print("="*60)

    sections = get_contents(base_url, token, course["id"])
    downloaded = 0
    downloaded_urls: Set[str] = set()
    course_code = short_course_code(course.get("shortname", ""))

    # Pre-scan: download unit guide PDFs to course root.
    # Unit guides live in non-week sections (e.g. "General") that the main
    # loop skips, so we catch them here before per-section processing.
    for section in sections:
        for mod in section.get("modules", []):
            if mod.get("modname") != "url":
                continue
            contents = mod.get("contents", [])
            if not contents:
                continue
            fileurl = contents[0].get("fileurl", "")
            if _is_unit_guide_url(fileurl):
                print(f"\n  Unit Guide: {mod.get('name', 'Unit Guide')}")
                if handle_unit_guide(
                    fileurl, course_dir, dry_run, output_dir,
                    hash_index, downloaded_urls,
                ):
                    downloaded += 1

    for section_index, section in enumerate(sections):
        sec_name = clean_section_name(section.get("name", ""))
        modules = section.get("modules", [])
        if not modules:
            continue

        if is_assessment_section(sec_name):
            section_dir = course_dir / "Assessments"
            week_label = "Assessments"
        else:
            week_folder = section_to_week_folder(sec_name)
            if week_folder:
                section_dir = course_dir / week_folder
                week_label = week_folder
            else:
                folder = section_folder_name(sec_name, section_index)
                section_dir = course_dir / folder
                week_label = folder

        print(f"\n  Section: {sec_name or week_label}")

        section_state: dict = {
            "rows": [],
            "misc_rows": [],
            "external_links": [],
            "skipped": [],
            "content_blocks": [],
        }

        summary_html = section.get("summary", "") or ""
        if summary_html.strip():
            summary_text_lower = re.sub(r'<[^>]+>', ' ', summary_html).lower()
            summary_is_misc = any(s in summary_text_lower for s in MISC_LABEL_NAME_SUBSTRINGS)
            if not summary_is_misc:
                md_summary = html_to_markdown(summary_html)
                if not is_content_block_skippable(md_summary):
                    summary_heading = extract_heading_from_html(summary_html, sec_name)
                    section_state["content_blocks"].append(
                        (summary_heading, "section summary", md_summary)
                    )

        for mod in modules:
            modname = mod.get("modname", "")

            if modname in ("resource", "folder", "page"):
                downloaded += handle_contents_module(
                    token, mod, section_dir, dry_run, downloaded_urls,
                    output_dir, hash_index, section_state,
                    misc_patterns, skip_patterns,
                )
            elif modname == "label":
                downloaded += handle_label_module(
                    token, mod, section_dir, dry_run, downloaded_urls,
                    output_dir, hash_index, section_state,
                    misc_patterns, skip_patterns,
                )
            elif modname == "url":
                downloaded += handle_url_module(
                    token, mod, section_dir, dry_run, downloaded_urls,
                    output_dir, hash_index, section_state,
                    misc_patterns, skip_patterns,
                )
            elif modname in ("quiz", "assign"):
                mod_name = mod.get("name", "").strip()
                description = mod.get("description", "")
                md_body = html_to_markdown(description) if description else ""
                if not md_body:
                    md_body = f"*{modname.title()}: {mod_name}*"
                heading = mod_name or f"({modname.title()})"
                section_state["content_blocks"].append((heading, modname, md_body))

        write_links_file(section_dir, week_label, section_state["external_links"], dry_run, output_dir)
        wrote_content = write_section_content(
            section_dir, sec_name or week_label, course_code,
            section_state["content_blocks"], dry_run, output_dir,
        )
        write_section_index(
            section_dir, week_label, section_state, dry_run, output_dir,
            has_content_md=wrote_content,
        )

    print(f"\n  Total downloaded: {downloaded} file(s)")


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        print("Copy config.example.yaml to config.yaml and edit it.")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        print(f"Config file is empty or malformed: {config_path}")
        sys.exit(1)
    return cfg


def resolve_path(value: str, base: Path) -> Path:
    """Resolve a config path: absolute paths kept, relative resolved against base."""
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def load_token(arg_token: Optional[str], cfg: dict, config_path: Path) -> str:
    """Token precedence: --token > token_file > MOODLE_TOKEN env var."""
    if arg_token:
        return arg_token.strip()

    token_file_value = cfg.get("moodle", {}).get("token_file")
    if token_file_value:
        token_file = resolve_path(token_file_value, config_path.parent)
        if token_file.exists():
            content = token_file.read_text().strip()
            if content:
                return content

    env_token = os.environ.get("MOODLE_TOKEN", "").strip()
    if env_token:
        return env_token

    print("No token provided.")
    print("Provide one via --token, the token_file in config.yaml, or the MOODLE_TOKEN env var.")
    sys.exit(1)


def parse_pattern_list(cfg: dict, key: str) -> List[str]:
    """
    Parse an optional list-of-strings field from config (misc_patterns,
    skip_patterns). Returns [] if missing/empty/wrong type. Non-string
    entries are dropped with a warning printed to stderr so the run can
    still proceed.
    """
    raw = cfg.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        print(f"Config warning: '{key}' must be a list of regex strings; ignoring.",
              file=sys.stderr)
        return []
    cleaned: List[str] = []
    for i, item in enumerate(raw):
        if isinstance(item, str) and item.strip():
            cleaned.append(item)
        else:
            print(f"Config warning: {key}[{i}] is not a non-empty string; ignored.",
                  file=sys.stderr)
    return cleaned


def parse_courses(cfg: dict) -> List[Dict[str, str]]:
    """Parse the courses list from config into [{code, folder}, ...]."""
    courses = cfg.get("courses", [])
    if not isinstance(courses, list) or not courses:
        print("Config error: 'courses' must be a non-empty list.")
        sys.exit(1)
    result: List[Dict[str, str]] = []
    for i, c in enumerate(courses):
        if not isinstance(c, dict):
            print(f"Config error: courses[{i}] must be a mapping with 'code' and 'folder'.")
            sys.exit(1)
        code = c.get("code")
        folder = c.get("folder") or code
        if not code:
            print(f"Config error: courses[{i}] is missing 'code'.")
            sys.exit(1)
        result.append({"code": str(code), "folder": str(folder)})
    return result


def run_setup():
    """Interactive setup wizard for first-time configuration."""
    # ---- Step 1: Welcome ----
    print()
    print("=" * 60)
    print("  course-sync setup wizard")
    print("=" * 60)
    print()
    print("This tool downloads and organises your course files from")
    print("Moodle (e.g. iLearn) into neat local folders.")
    print()
    print("You will need:")
    print("  1. Your Moodle site URL")
    print("  2. A web service token (from your Moodle profile)")
    print()

    config_path = SCRIPT_DIR / "config.yaml"
    token_path = SCRIPT_DIR / ".course_sync_token"

    # ---- Steps 2-4: Moodle URL + Token + Connection test (with retry) ----
    base_url = ""
    token = ""
    user_id = None
    fullname = ""

    while True:
        # Step 2: Moodle URL
        print("Step 1: Moodle URL")
        print("-" * 40)
        url_input = input("Enter your Moodle URL (e.g. https://ilearn.mq.edu.au): ").strip()
        if not url_input:
            print("  URL cannot be empty. Please try again.\n")
            continue
        if not url_input.startswith("http://") and not url_input.startswith("https://"):
            print("  URL must start with http:// or https://. Please try again.\n")
            continue
        base_url = url_input.rstrip("/")
        print()

        # Step 3: Token
        print("Step 2: Web service token")
        print("-" * 40)
        print("To find your token:")
        print("  1. Log in to Moodle in your browser")
        print("  2. Go to: Profile -> Preferences -> Security keys")
        print("  3. Copy the token next to 'Moodle mobile web service'")
        print()
        token_input = input("Paste your token here: ").strip()
        if not token_input:
            print("  Token cannot be empty. Please try again.\n")
            continue
        token = token_input

        # Save token to file
        token_path.write_text(token + "\n", encoding="utf-8")
        print(f"  Token saved to {token_path.name}")
        print()

        # Step 4: Test connection
        print("Step 3: Testing connection...")
        try:
            user_id, fullname, _site_info = get_user_id(base_url, token)
            print(f"  Connected as: {fullname}")
            print()
            break
        except Exception as e:
            print(f"  Connection failed: {e}")
            print()
            retry = input("Would you like to re-enter your URL and token? (y/n): ").strip().lower()
            if retry not in ("y", "yes"):
                print("\nSetup cancelled. You can run --setup again later.")
                sys.exit(0)
            print()

    # ---- Step 5: List and select courses ----
    print("Step 4: Select courses to sync")
    print("-" * 40)
    print("Fetching your enrolled courses...")
    courses = get_courses(base_url, token, user_id)

    if not courses:
        print("  No enrolled courses found. Check your Moodle enrolment.")
        sys.exit(1)

    print()
    for i, c in enumerate(courses, 1):
        code = short_course_code(c.get("shortname", ""))
        print(f"  {i:3d}. [{code}] {c.get('fullname', '(unnamed)')}")
    print()
    print("Enter the numbers of the courses you want to sync.")
    print('Examples: "1,3,5" or "1 3 5" or "all"')
    print()

    selected_courses = []  # type: List[dict]
    while True:
        selection = input("Your selection: ").strip()
        if not selection:
            print("  Please enter at least one course number or 'all'.")
            continue

        if selection.lower() == "all":
            selected_courses = list(courses)
            break

        # Parse comma- or space-separated numbers
        tokens = selection.replace(",", " ").split()
        valid = True
        indices = []  # type: List[int]
        for t in tokens:
            if not t.isdigit():
                print(f"  '{t}' is not a valid number. Please try again.")
                valid = False
                break
            idx = int(t)
            if idx < 1 or idx > len(courses):
                print(f"  {idx} is out of range (1-{len(courses)}). Please try again.")
                valid = False
                break
            indices.append(idx)

        if not valid:
            continue
        if not indices:
            print("  Please enter at least one course number.")
            continue

        selected_courses = [courses[i - 1] for i in indices]
        break

    print()
    print(f"  Selected {len(selected_courses)} course(s):")
    for c in selected_courses:
        print(f"    - {short_course_code(c.get('shortname', ''))} ({c.get('fullname', '')})")
    print()

    # ---- Step 6: Output directory ----
    print("Step 5: Output directory")
    print("-" * 40)
    default_dir = "~/Documents/Courses"
    dir_input = input(f"Where should files be saved? [{default_dir}]: ").strip()
    if not dir_input:
        dir_input = default_dir

    output_dir = Path(dir_input).expanduser().resolve()

    if not output_dir.exists():
        print(f"  Directory does not exist: {output_dir}")
        create = input("  Create it now? (y/n): ").strip().lower()
        if create in ("y", "yes"):
            output_dir.mkdir(parents=True, exist_ok=True)
            print(f"  Created: {output_dir}")
        else:
            print("  Directory was not created. You can create it manually before syncing.")
    else:
        print(f"  Using: {output_dir}")
    print()

    # ---- Step 7: Write config.yaml ----
    print("Step 6: Writing configuration")
    print("-" * 40)

    course_entries = []  # type: List[Dict[str, str]]
    for c in selected_courses:
        code = short_course_code(c.get("shortname", ""))
        course_entries.append({"code": code, "folder": code})

    config_data = {
        "moodle": {
            "base_url": base_url,
            "token_file": ".course_sync_token",
        },
        "output_dir": dir_input,
        "courses": course_entries,
    }

    if config_path.exists():
        print(f"  Warning: {config_path.name} already exists.")
        overwrite = input("  Overwrite it? (y/n): ").strip().lower()
        if overwrite not in ("y", "yes"):
            print("  Config was NOT overwritten. Your existing config is unchanged.")
            print()
            print("Setup complete (config not written).")
            print()
            print("To preview what would be synced:")
            print("  python3 course_sync.py --dry-run")
            print()
            print("To sync your files:")
            print("  python3 course_sync.py")
            return

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)
    print(f"  Config written to {config_path.name}")
    print()

    # ---- Step 8: Done ----
    print("=" * 60)
    print("  Setup complete!")
    print("=" * 60)
    print()
    print("To preview what would be synced (no files downloaded):")
    print("  python3 course_sync.py --dry-run")
    print()
    print("To sync your files:")
    print("  python3 course_sync.py")
    print()


def main():
    parser = argparse.ArgumentParser(description="Sync course files to local folders")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help="Path to config YAML (default: ./config.yaml next to script)")
    parser.add_argument("--token", default=None,
                        help="Moodle web service token (overrides config and env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without downloading")
    parser.add_argument("--setup", action="store_true",
                        help="Launch interactive setup wizard for first-time configuration")
    args = parser.parse_args()

    if args.setup:
        try:
            run_setup()
        except KeyboardInterrupt:
            print("\n\nSetup cancelled. You can run --setup again any time.")
            sys.exit(0)
        return

    config_path = Path(args.config).expanduser().resolve()
    cfg = load_config(config_path)

    moodle_cfg = cfg.get("moodle", {})
    base_url = moodle_cfg.get("base_url", "").rstrip("/")
    if not base_url:
        print("Config error: 'moodle.base_url' is required.")
        sys.exit(1)

    output_dir_value = cfg.get("output_dir")
    if not output_dir_value:
        print("Config error: 'output_dir' is required.")
        sys.exit(1)
    output_dir = resolve_path(output_dir_value, config_path.parent)

    course_specs = parse_courses(cfg)
    course_folder_map = {c["code"]: c["folder"] for c in course_specs}

    token = load_token(args.token, cfg, config_path)

    print("Connecting...")
    try:
        user_id, fullname, site_info = get_user_id(base_url, token)
    except Exception as e:
        print(f"Error: Could not authenticate. Check your token.\n{e}")
        sys.exit(1)

    version_display, build_number = parse_moodle_version(site_info)
    sitename = str(site_info.get("sitename", "")).strip()

    if version_display and sitename:
        print(f"Logged in as: {fullname} ({sitename}, Moodle {version_display})")
    elif version_display:
        print(f"Logged in as: {fullname} (Moodle {version_display})")
    elif sitename:
        print(f"Logged in as: {fullname} ({sitename})")
    else:
        print(f"Logged in as: {fullname}")

    if build_number is not None and build_number < MOODLE_39_BUILD:
        warn_version = version_display or str(build_number)
        print(
            f"Warning: This Moodle instance is running version {warn_version}, "
            "which is below the tested minimum (3.9). Some features may not work."
        )

    courses = get_courses(base_url, token, user_id)

    def match_course(shortname: str) -> Optional[str]:
        for code, folder in course_folder_map.items():
            if shortname.startswith(code):
                return folder
        return None

    matched = [(c, match_course(c["shortname"])) for c in courses]
    matched = [(c, folder) for c, folder in matched if folder]

    if not matched:
        print("\nNo matching courses found. Your enrolled courses:")
        for c in courses:
            print(f"  {c['shortname']:40} {c['fullname']}")
        print("\nUpdate 'courses' in your config.yaml to match.")
        sys.exit(1)

    hash_index = load_hash_index(output_dir)

    misc_patterns = parse_pattern_list(cfg, "misc_patterns")
    skip_patterns = parse_pattern_list(cfg, "skip_patterns")

    for course, folder_name in matched:
        course_dir = output_dir / folder_name
        sync_course(
            base_url, token, course, course_dir, args.dry_run,
            output_dir, hash_index, misc_patterns, skip_patterns,
        )
        if not args.dry_run:
            save_hash_index(output_dir, hash_index)

    print("\nDone.")


if __name__ == "__main__":
    main()

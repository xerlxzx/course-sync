#!/usr/bin/env python3
"""
moodle-sync (course-sync): downloads and organises files from a Moodle instance.

Usage (from the repo root):
    python3 src/course_sync.py [--config PATH] [--token TOKEN] [--dry-run] [--setup]

Config: see config.example.yaml. Companion tools in this repo: src/sway_sync.py
(exports the Sway decks that the _links.md files point at), src/sync_one.py
(re-fetches one module) and scripts/sync_all.sh (runs this, then sway_sync.py).
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import traceback
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import requests
import yaml
from markdownify import markdownify as _markdownify


# This file lives in <repo>/src/. The repo root is the runtime root: config.yaml
# and the token file sit there, and relative paths in config.yaml (token_file,
# output_dir) resolve against it.
REPO_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_DIR / "config.yaml"

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


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Seconds before a Moodle web service call gives up.
API_TIMEOUT = 60

# Token values seen so far in this process (see register_secret). Anything that
# reaches stdout, stderr or a generated file goes through redact() or
# strip_secrets(), because requests puts the full URL, token included, into the
# text of its exceptions.
_SECRETS: Set[str] = set()
_MIN_SECRET_LEN = 8
_TOKEN_PARAM_RE = re.compile(r"(?i)((?:ws)?token=)[^&\s\"'<>)\]]+")
_REDACTED = "***"


def register_secret(value: Optional[str]) -> None:
    """Remember a secret (the Moodle token) so redact() can strip it from any text."""
    if value and len(value) >= _MIN_SECRET_LEN:
        _SECRETS.add(value)
        _SECRETS.add(quote(value, safe=""))


def strip_secrets(text: str) -> str:
    """Replace every registered secret value in ``text``. Safe for prose."""
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, _REDACTED)
    return text


def redact(text) -> str:
    """
    Make ``text`` safe to print or write: strip registered secret values and the
    value of any ``token=`` / ``wstoken=`` query parameter. Accepts exceptions.
    """
    return _TOKEN_PARAM_RE.sub(r"\1" + _REDACTED, strip_secrets(str(text)))


def describe_error(exc: BaseException) -> str:
    """One-line, redacted ``ClassName: message`` for logs and _index.md."""
    return redact(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Moodle web service API
# ---------------------------------------------------------------------------

def api(base_url: str, token: str, function: str, **params):
    register_secret(token)
    url = f"{base_url}/webservice/rest/server.php"
    try:
        r = requests.get(url, params={
            "wstoken": token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
            **params,
        }, timeout=API_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        # from None: the original exception text carries the full URL and token.
        raise RuntimeError(f"Moodle API call {function} failed: {describe_error(e)}") from None
    if isinstance(data, dict) and "exception" in data:
        raise RuntimeError(f"Moodle API error: {redact(data.get('message', data))}")
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


# ---------------------------------------------------------------------------
# Section routing and name cleaning
# ---------------------------------------------------------------------------

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


def section_dir_for(course_dir: Path, sec_name: str, section_index: int) -> Path:
    """
    Local folder for a Moodle section. sec_name is the cleaned section
    name (see clean_section_name).

    Precedence: assessment-like names go to Assessments/ (so "Week 3
    Quiz" is an assessment, not a week), then "Week N" names to WeekN/,
    then everything else to a folder derived from the name or position
    (section_folder_name). The folder's own name doubles as the section's
    display label in _index.md and _links.md.
    """
    if is_assessment_section(sec_name):
        return course_dir / "Assessments"
    week_folder = section_to_week_folder(sec_name)
    if week_folder:
        return course_dir / week_folder
    return course_dir / section_folder_name(sec_name, section_index)


# Longest filename (UTF-8 bytes) sanitize_filename returns. Filesystems cap a name
# at 255 bytes; the margin leaves room for the ".md" the page and book handlers
# append, a " (N)" collision suffix and the ".part" temp name.
MAX_NAME_BYTES = 200


def _split_ext(name: str) -> Tuple[str, str]:
    """Split ``name`` into (stem, extension); only a short, space-free extension counts."""
    stem, ext = os.path.splitext(name)
    if not 1 < len(ext.encode("utf-8")) <= 16 or " " in ext:
        return name, ""
    return stem, ext


def _shorten_name(name: str) -> str:
    """
    Cut ``name`` to MAX_NAME_BYTES of UTF-8 without splitting a character,
    keeping a short extension and adding "~<8 hex>" (a hash of the whole name)
    so two long names that share a prefix stay distinct. Names that already fit
    are returned unchanged. Deterministic, so reruns pick the same name.
    """
    raw = name.encode("utf-8")
    if len(raw) <= MAX_NAME_BYTES:
        return name
    stem, ext = _split_ext(name)
    suffix = "~" + hashlib.sha256(raw).hexdigest()[:8]
    budget = MAX_NAME_BYTES - len((suffix + ext).encode("utf-8"))
    head = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip().rstrip(".")
    return head + suffix + ext


def sanitize_filename(name: str) -> str:
    """
    Strip cross-platform-unsafe characters from a filename.

    Removes: : ? * | < > \\ /
    Plus leading/trailing whitespace and trailing dots.
    Collapses internal whitespace to single spaces.
    Names longer than MAX_NAME_BYTES are shortened by _shorten_name.
    Always returns a non-empty string ("untitled" as fallback).
    """
    if not name:
        return "untitled"
    cleaned = re.sub(r'[:?*|<>\\/]', '', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = cleaned.rstrip('.')
    cleaned = cleaned.strip()
    return _shorten_name(cleaned) if cleaned else "untitled"


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

    Moodle shortnames often look like ``UNIT1000_T2_2026_ALL``.
    For headings in ``_content.md`` we want just ``UNIT1000``. The heuristic
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
# Download URLs and display paths
# ---------------------------------------------------------------------------

def _origin(url: str) -> Optional[Tuple[str, str, int]]:
    """(scheme, hostname, port) of an absolute URL, or None if it has no host."""
    try:
        p = urlparse(url)
        scheme = (p.scheme or "").lower()
        host = (p.hostname or "").lower()
        port = p.port or {"http": 80, "https": 443}.get(scheme)
    except ValueError:
        return None
    if not scheme or not host:
        return None
    return scheme, host, port


def is_moodle_file_url(url: str, base_url: str) -> bool:
    """
    True only for a pluginfile URL on the configured Moodle site: same scheme,
    hostname and port as ``base_url`` and ``/pluginfile.php/`` in the path.

    This is the gate for the Moodle token. Anything else (another university's
    Moodle, a CDN, a redirect target) is an external link and must never be sent
    the token.
    """
    origin = _origin(url)
    return (origin is not None and origin == _origin(base_url)
            and "/pluginfile.php/" in urlparse(url).path)


def _where(url: str) -> str:
    """host + path of a URL for messages: no query string, so no token."""
    p = urlparse(url)
    return f"{p.hostname or '?'}{p.path}"


def make_download_url(url: str, token: str, base_url: str) -> str:
    """
    Convert a Moodle pluginfile URL to the webservice download form and append
    the token. Raises ValueError for any URL that is not a pluginfile URL on
    ``base_url``'s own site, so the token cannot be attached to a foreign host.
    """
    register_secret(token)
    if not is_moodle_file_url(url, base_url):
        raise ValueError(
            f"refusing to attach the Moodle token to {_where(url)}: "
            "not a file URL on the configured Moodle site")
    if "/webservice/pluginfile.php/" not in url:
        dl_url = url.replace("/pluginfile.php/", "/webservice/pluginfile.php/")
    else:
        dl_url = url
    if "token=" not in dl_url:
        sep = "&" if "?" in dl_url else "?"
        dl_url = f"{dl_url}{sep}token={token}"
    return dl_url


MAX_REDIRECTS = 5
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def fetch_moodle_file(base_url: str, token: str, url: str, timeout: int):
    """
    GET a Moodle file with the token attached and return the response.

    Redirects are followed by hand, at most MAX_REDIRECTS of them, and only to
    another pluginfile URL on the same site (same scheme, host and port): the
    token travels in the query string, so letting requests follow a redirect to
    another host would hand it over. Anything else raises RuntimeError.
    """
    target = make_download_url(url, token, base_url)
    for _ in range(MAX_REDIRECTS + 1):
        r = requests.get(target, timeout=timeout, allow_redirects=False)
        if r.status_code in _REDIRECT_STATUSES:
            location = r.headers.get("Location")
            if not location:
                raise RuntimeError(f"HTTP {r.status_code} redirect without a Location")
            target_next = urljoin(target, location)
            if not is_moodle_file_url(target_next, base_url):
                raise RuntimeError(
                    f"refused a redirect to {_where(target_next)}: "
                    "tokenised requests stay on the Moodle site")
            target = make_download_url(target_next, token, base_url)
            continue
        r.raise_for_status()
        if 300 <= r.status_code < 400:
            raise RuntimeError(f"unexpected HTTP {r.status_code}")
        return r
    raise RuntimeError(f"gave up after {MAX_REDIRECTS} redirects")


# Names the tool itself writes into every section folder.
GENERATED_NAMES = frozenset(["_index.md", "_links.md", "_content.md"])


def _fold_name(name: str) -> str:
    """Key under which two names count as the same file: case- and Unicode-normalisation-blind."""
    return unicodedata.normalize("NFC", name).casefold()


def file_source_key(fileurl: str) -> str:
    """
    Identity of the file behind a Moodle URL: host and pluginfile path, without
    the query string or the /webservice prefix, so the same file reached through
    two URL spellings is one source.
    """
    p = urlparse(fileurl)
    path = p.path.replace("/webservice/pluginfile.php/", "/pluginfile.php/")
    return f"file:{(p.hostname or '').lower()}{unquote(path)}"


def claim_name(section_state: dict, dest: Path, filename: str, source_key: str) -> str:
    """
    Reserve ``filename`` in folder ``dest`` for the file identified by
    ``source_key`` and return the name to use.

    Names are compared case-insensitively whatever the filesystem is, so a run
    behaves the same on APFS and on Linux. The first source to claim a name keeps
    it; a different source whose name differs only by case (or is identical)
    gets "<stem> (2)<ext>", then "(3)" and so on. The same source claiming again
    gets the same name back. Numbering follows the order the server lists the
    modules, so names are stable across runs while that order is. The claims are
    shared by every section of the course (several sections can map to one
    folder, e.g. Assessments/), and the tool's own _index.md, _links.md and
    _content.md are never handed out.
    """
    claims = section_state["name_claims"]
    stem, ext = _split_ext(filename)
    n = 1
    while True:
        candidate = filename if n == 1 else f"{stem} ({n}){ext}"
        key = (str(dest), _fold_name(candidate))
        owner = claims.get(key)
        if owner is None and key[1] in GENERATED_NAMES:
            owner = "<generated>"
        if owner is None:
            claims[key] = source_key
            return candidate
        if owner == source_key:
            return candidate
        n += 1


def _rel_for_display(path: Path, output_dir: Path) -> str:
    """Return a path string relative to output_dir if possible, else absolute."""
    try:
        return str(path.relative_to(output_dir))
    except ValueError:
        return str(path)


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

def load_hash_index(output_dir: Path, move_aside: bool = True) -> dict:
    """
    Load the cross-run hash index from <output_dir>/.course_sync/downloaded.json.

    A file that cannot be used is renamed aside (``move_aside``; a dry run passes
    False so it writes nothing) and an empty index is returned, with a warning.
    """
    index_path = output_dir / INDEX_DIRNAME / INDEX_FILENAME
    if not index_path.exists():
        return {"files": {}}
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
        problem = "unexpected structure"
    except (ValueError, OSError) as e:  # ValueError covers bad JSON and bad UTF-8
        problem = f"{type(e).__name__}: {e}"

    # Starting empty silently would make the next run treat every file as new
    # and, once saved, overwrite the evidence. Keep the bad file and say so.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = index_path.with_name(f"{INDEX_FILENAME}.corrupt-{stamp}")
    if not move_aside:
        kept = f"Dry run: it was left in place; a real run moves it to {backup.name}."
    else:
        try:
            os.replace(index_path, backup)
            kept = f"It was moved to {backup.name}."
        except OSError as e:
            kept = f"It could not be moved aside ({e})."
    print(f"WARNING: the dedup index {index_path} is unusable ({problem}). {kept}\n"
          "         Starting with an empty index: files on disk are kept, but content "
          "duplicates are re-detected on this run.", file=sys.stderr)
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


def forget_hash_path(index: dict, rel_path: str, keep_sha: str) -> None:
    """
    Drop rel_path from every hash entry except keep_sha.

    Used when a file is replaced in place: the old content no longer lives at
    that path, and leaving the claim behind would make tier-2 dedup point future
    duplicates at a path that holds different bytes. Hash entries left with no
    paths are removed outright.
    """
    files = index.get("files", {})
    folded = _fold_name(rel_path)
    for sha, entry in list(files.items()):
        if sha == keep_sha:
            continue
        paths = entry.get("paths", [])
        # Compare case-insensitively: on a case-insensitive filesystem a claim
        # recorded as "Sample output.txt" is a claim on "sample output.txt".
        kept = [p for p in paths if _fold_name(p) != folded]
        if len(kept) != len(paths):
            entry["paths"] = kept
            if not kept:
                del files[sha]


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
        paths = entry.setdefault("paths", [])
        if _fold_name(rel_path) not in [_fold_name(p) for p in paths]:
            paths.append(rel_path)


# ---------------------------------------------------------------------------
# File download with two-tier dedup
# ---------------------------------------------------------------------------

def record_dup(
    index: dict,
    dest_rel: str,
    sha256: str,
    size: int,
    original: str,
    remote_size: Optional[int],
    remote_mtime: Optional[int],
) -> None:
    """
    Remember that the file at ``dest_rel`` is a content duplicate of ``original``
    (so it was never written), together with the server metadata seen. Lets later
    runs skip the fetch while neither side has changed.
    """
    index.setdefault("dups", {})[dest_rel] = {
        "sha256": sha256,
        "size": size,
        "original": original,
        "remote_size": remote_size,
        "remote_mtime": remote_mtime,
    }


def forget_dup(index: dict, dest_rel: str) -> None:
    """Drop the duplicate record for ``dest_rel`` (it now has, or needs, its own file)."""
    index.get("dups", {}).pop(dest_rel, None)


def known_dup_original(
    index: dict,
    output_dir: Path,
    dest_rel: str,
    remote_size: Optional[int],
    remote_mtime: Optional[int],
) -> Optional[str]:
    """
    If ``dest_rel`` is a recorded duplicate that is still valid, return the path
    of the file it duplicates; otherwise None (and drop a record that no longer
    holds).

    Valid means the server metadata equals what was recorded (links scraped from
    label HTML carry none, on both sides, which counts as equal: trust the
    record, like tier 1b does) and a copy of the content is still on disk.
    """
    rec = index.get("dups", {}).get(dest_rel)
    if rec is None:
        return None
    if rec.get("remote_size") == remote_size and rec.get("remote_mtime") == remote_mtime:
        holder = _live_holder(index, rec.get("sha256", ""), rec.get("size", -1), output_dir)
        if holder is not None:
            return holder
    forget_dup(index, dest_rel)
    return None


def _live_holder(hash_index: dict, sha256: str, size: int, output_dir: Path) -> Optional[str]:
    """
    Return a recorded path that really holds the content ``sha256``, or None.

    Existence is not enough: on a case-insensitive filesystem a claim on
    "Sample output.txt" is satisfied by a different file called "sample
    output.txt", and an in-place update can change what a path holds. So the
    size must match and the bytes are hashed. Recorded paths that fail the check
    are dropped from the entry, which is how stale index records get cleaned up.
    """
    files = hash_index.get("files", {})
    entry = files.get(sha256)
    if entry is None:
        return None
    holders, stale = [], []
    for rel in entry.get("paths", []):
        candidate = output_dir / rel
        try:
            ok = (candidate.is_file() and candidate.stat().st_size == size
                  and hashlib.sha256(candidate.read_bytes()).hexdigest() == sha256)
        except OSError:
            ok = False
        (holders if ok else stale).append(rel)
    if stale:
        if holders:
            entry["paths"] = holders
        else:
            del files[sha256]
    return holders[0] if holders else None


def _remote_looks_newer(
    local_stat: os.stat_result,
    remote_size: Optional[int],
    remote_mtime: Optional[int],
) -> bool:
    """
    Decide whether a file already on disk might be an outdated copy.

    Moodle lets staff replace a file in place, keeping the same name and URL, so
    "a file with this name exists" does not mean "we hold the current version".
    A size mismatch is conclusive; a remote timestamp newer than our local file
    means the server copy changed after we fetched it. When the caller has no
    metadata (e.g. a link scraped out of label HTML) both args are None and we
    keep the old behaviour of trusting the file on disk.
    """
    if remote_size is not None and remote_size != local_stat.st_size:
        return True
    if remote_mtime and remote_mtime > local_stat.st_mtime:
        return True
    return False


def _write_bytes(path: Path, body: bytes) -> None:
    """
    Write ``body`` to ``path`` through a temp file and a rename, so a failure
    part-way (disk full, interrupted run) never leaves a truncated file behind
    that a later run would trust as already downloaded.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def download_file(
    base_url: str,
    token: str,
    fileurl: str,
    filename: str,
    dest: Path,
    dry_run: bool,
    downloaded_urls: Set[str],
    output_dir: Path,
    hash_index: dict,
    remote_size: Optional[int] = None,
    remote_mtime: Optional[int] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Download a file with tiered dedup (see DEV_GUIDE section 5). Returns
    (outcome, dest_rel) where:

      outcome is one of:
        "downloaded": bytes fetched and written to disk (new file, or an
                      in-place server update replacing an older local copy)
        "skipped":    already known via per-run URL set, or the copy on disk is
                      already current
        "dup":        content already on disk under another path (not written);
                      either fetched and hashed just now, or recorded as a
                      duplicate by an earlier run and not fetched at all
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

    # Tier 1b: same path already on disk. Only skip if the remote metadata
    # agrees we already hold the current version; an in-place re-upload keeps
    # the same name and URL, so existence alone proves nothing.
    local_stat = dest_path.stat() if dest_path.exists() else None
    if local_stat is not None:
        if not _remote_looks_newer(local_stat, remote_size, remote_mtime):
            print(f"  [skip] {dest_rel}")
            return "skipped", dest_rel
        if dry_run:
            print(f"  [dry-upd] {dest_rel}  (changed on server)")
            return "dry", dest_rel
        # Fall through to re-fetch so we can compare content, not just metadata.
    else:
        # Tier 1c: a content duplicate is never written at its own path, so tier
        # 1b cannot see it. The index remembers it; skip the fetch while the
        # server metadata is unchanged and the original is still on disk.
        original = known_dup_original(hash_index, output_dir, dest_rel, remote_size, remote_mtime)
        if original is not None:
            print(f"  [dup]  {dest_rel}  (same content as {original}; recorded, not re-fetched)")
            return "dup", dest_rel

    if dry_run:
        print(f"  [dry]  {dest_rel}")
        return "dry", dest_rel

    # Fetch bytes into memory so we can hash before writing
    r = fetch_moodle_file(base_url, token, fileurl, timeout=120)
    body = r.content
    sha256 = hashlib.sha256(body).hexdigest()
    size = len(body)

    # Replacing a file we already hold: the metadata said it changed, so trust
    # the bytes. This runs before tier 2 because the new content may coincide
    # with some other file's hash, and we still want it written to this path.
    if local_stat is not None:
        if hashlib.sha256(dest_path.read_bytes()).hexdigest() == sha256:
            # Same bytes under a newer server timestamp (a re-upload). Stamp the
            # local file with the server's time so the next run's mtime check
            # passes without another GET; otherwise a big file is re-fetched
            # every run.
            stamp = remote_mtime or datetime.now().timestamp()
            try:
                os.utime(dest_path, (local_stat.st_atime, stamp))
            except OSError:
                pass
            print(f"  [skip] {dest_rel}  (metadata changed, content identical)")
            return "skipped", dest_rel
        print(f"  [upd]  {dest_rel}  ({local_stat.st_size} -> {size} bytes)")
        _write_bytes(dest_path, body)
        forget_hash_path(hash_index, dest_rel, sha256)
        record_hash_path(hash_index, sha256, size, dest_rel)
        forget_dup(hash_index, dest_rel)
        return "downloaded", dest_rel

    # Tier 2: cross-run hash dedup
    holder = _live_holder(hash_index, sha256, size, output_dir)
    if holder is not None:
        print(f"  [dup]  {dest_rel}  (same content as {holder})")
        record_dup(hash_index, dest_rel, sha256, size, holder, remote_size, remote_mtime)
        return "dup", dest_rel
    # No recorded copy still holds these bytes (deleted by the user, or replaced
    # by other content): the index entry was stale, so write the file.

    # Write the file
    print(f"  [dl]   {dest_rel}")
    _write_bytes(dest_path, body)
    record_hash_path(hash_index, sha256, size, dest_rel)
    forget_dup(hash_index, dest_rel)
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


def _place_file(
    base_url: str,
    token: str,
    fileurl: str,
    filename: str,
    mod_name: str,
    modname: str,
    section_dir: Path,
    dry_run: bool,
    downloaded_urls: Set[str],
    output_dir: Path,
    hash_index: dict,
    section_state: dict,
    misc_patterns: List[str],
    skip_patterns: List[str],
    misc_reason: Optional[str] = None,
    remote_size: Optional[int] = None,
    remote_mtime: Optional[int] = None,
) -> Optional[str]:
    """
    Shared tail of every handler that downloads a file: apply skip_patterns
    (a config-driven hard skip: never downloaded, never recorded on disk),
    route noise to ``_misc/`` (or a ``LessonN/`` subfolder), download, and
    record the ``_index.md`` rows.

    ``modname`` is the type shown in the index table. ``misc_reason`` forces
    misc routing regardless of filename (label modules flagged as boilerplate).
    ``remote_size`` / ``remote_mtime`` are the server's metadata for the file,
    when the API provides it, and decide whether an on-disk copy is stale.

    Returns download_file()'s outcome, or None when nothing was fetched
    (skip rule matched, or no filename).
    """
    if should_skip_file(filename, skip_patterns):
        section_state["skipped"].append(f"{filename} (matched skip_patterns)")
        print(f"  [skip-rule] {filename} (matched skip_patterns)")
        return None

    misc_reason = misc_reason or is_misc_file(filename, misc_patterns)
    if misc_reason:
        dest = section_dir / MISC_SUBFOLDER
    else:
        lesson = lesson_subfolder_from_filename(filename)
        dest = section_dir / lesson if lesson else section_dir

    if fileurl not in downloaded_urls:
        # A different file with the same name (or the same name in another case)
        # in this folder gets "name (2).ext" instead of overwriting the first.
        filename = claim_name(section_state, dest, filename, file_source_key(fileurl))

    try:
        outcome, dest_rel = download_file(
            base_url, token, fileurl, filename, dest, dry_run, downloaded_urls, output_dir,
            hash_index, remote_size=remote_size, remote_mtime=remote_mtime,
        )
    except Exception as e:
        # One bad file (404, timeout, full disk) must not abort the module's other
        # files, the section's index files or the rest of the run.
        record_failure(section_state, _rel_for_display(dest / filename, output_dir),
                       describe_error(e))
        return None
    if outcome is None:
        return None
    if misc_reason:
        print(f"  [misc]  {dest_rel}  (noise: {misc_reason})")
        section_state["misc_rows"].append(
            (collapse_ws(mod_name) or "(unnamed)", modname, filename, misc_reason)
        )
    _row(section_state["rows"], mod_name, modname, filename, _dest_label(dest, section_dir))
    return outcome


def record_failure(section_state: dict, label: str, reason: str) -> None:
    """
    Log a ``[fail]`` line and remember the failure in the section: in its skipped
    list (so _index.md shows it) and in its failure list (so the run exits 1).
    ``reason`` must already be redacted (see describe_error).
    """
    print(f"  [fail] {label}: {reason}")
    section_state["skipped"].append(f"{label} - FAILED: {reason}")
    section_state["failures"].append(f"{label}: {reason}")


def _write_text_if_changed(path: Path, content: str) -> bool:
    """
    Write ``content`` to ``path`` unless it already holds exactly that text,
    so unchanged captures stay byte-identical and reruns are idempotent.
    Creates the parent folder on demand. Returns True when the file was written.
    """
    content = strip_secrets(content)
    if path.exists() and path.read_text(encoding="utf-8", errors="replace") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def save_page_markdown(
    base_url: str,
    token: str,
    mod: dict,
    fileurl: str,
    section_dir: Path,
    dry_run: bool,
    output_dir: Path,
    section_state: dict,
) -> int:
    """
    Render a Moodle *page* module to Markdown.

    For page modules the ``index.html`` in contents[] IS the content - Moodle
    pages carry things like full assignment specifications - so it must be
    captured, not skipped as template noise. Output is deterministic (no
    timestamps) so unchanged pages produce byte-identical files and reruns
    stay idempotent. Returns 1 when a file was written/updated, else 0.
    """
    mod_name = collapse_ws(mod.get("name", "")) or "(unnamed page)"
    filename = claim_name(section_state, section_dir, sanitize_filename(mod_name) + ".md",
                          f"cm:{mod.get('id', id(mod))}")
    dest_path = section_dir / filename
    dest_rel = _rel_for_display(dest_path, output_dir)

    if dry_run:
        print(f"  [dry-page] {dest_rel}")
        _row(section_state["rows"], mod_name, "page", filename, "(root)")
        return 0

    try:
        r = fetch_moodle_file(base_url, token, fileurl, timeout=60)
        html_src = r.text
    except Exception as e:
        record_failure(section_state, dest_rel, f"page fetch failed: {describe_error(e)}")
        return 0

    md_body = html_to_markdown(html_src)
    content = (
        f"# {mod_name}\n\n"
        f"> Moodle page captured by course-sync. Check the live page on Moodle\n"
        f"> if formatting looks off - tables and images lose fidelity in Markdown.\n\n"
        f"{md_body}\n"
    )
    written = _write_text_if_changed(dest_path, content)
    print(f"  [page] {dest_rel}" if written else f"  [ok]   {dest_rel} (page unchanged)")
    _row(section_state["rows"], mod_name, "page", filename, "(root)")
    return 1 if written else 0


def handle_book_module(
    base_url: str,
    token: str,
    mod: dict,
    section_dir: Path,
    dry_run: bool,
    output_dir: Path,
    section_state: dict,
) -> int:
    """
    Render a Moodle *book* module (multi-chapter HTML) to one Markdown file.

    A book's contents[] holds one index.html per chapter under filepaths like
    "/1/", "/2/", plus a "structure" entry carrying chapter titles as JSON.
    Books often carry real content (e.g. solutions to the week's exercises),
    so they get captured like pages rather than warned about.
    """
    mod_name = collapse_ws(mod.get("name", "")) or "(unnamed book)"

    contents = mod.get("contents", [])
    chapters = []  # (sort_key, title, fileurl)
    titles = {}
    for c in contents:
        if c.get("filename") == "structure":
            try:
                for ch in json.loads(c.get("content") or "[]"):
                    href = str(ch.get("href", "")).split("/")[0]
                    titles[href] = _strip_tags(str(ch.get("title", ""))).strip()
            except Exception:
                pass
    for c in contents:
        if c.get("type") != "file" or c.get("filename", "").lower() != "index.html":
            continue
        chapter_id = (c.get("filepath") or "/").strip("/")
        try:
            sort_key = int(chapter_id)
        except ValueError:
            sort_key = 10**6
        chapters.append((sort_key, titles.get(chapter_id, f"Chapter {chapter_id or '?'}"),
                         c.get("fileurl", "")))
    if not chapters:
        # Books (and other modules) can be time-locked; Moodle then returns the
        # module without contents and uservisible=false. Not an error - the
        # chapters are captured by the first sync after the release date.
        if mod.get("uservisible") is False:
            info = _strip_tags(mod.get("availabilityinfo", "") or "not yet available")
            section_state["skipped"].append(f"{mod_name} (book) - locked: {info}")
            print(f"  [locked] book '{mod_name}' - {info}")
        else:
            section_state["skipped"].append(f"{mod_name} (book) - no chapters found")
            print(f"  [warn] book '{mod_name}' has no readable chapters")
        return 0
    chapters.sort()

    # Claim the name only now: a locked or empty book writes nothing, so it must
    # not take a name that another module could use.
    filename = claim_name(section_state, section_dir, sanitize_filename(mod_name) + ".md",
                          f"cm:{mod.get('id', id(mod))}")
    dest_path = section_dir / filename
    dest_rel = _rel_for_display(dest_path, output_dir)

    if dry_run:
        print(f"  [dry-book] {dest_rel} ({len(chapters)} chapter(s))")
        _row(section_state["rows"], mod_name, "book", filename, "(root)")
        return 0

    parts = [f"# {mod_name}\n\n"
             f"> Moodle book captured by course-sync ({len(chapters)} chapter(s)). Check the\n"
             f"> live page on Moodle if formatting looks off.\n"]
    failed_chapters = 0
    for _, title, fileurl in chapters:
        try:
            r = fetch_moodle_file(base_url, token, fileurl, timeout=60)
            body = html_to_markdown(r.text)
        except Exception as e:
            reason = describe_error(e)
            body = f"*(chapter fetch failed: {reason})*"
            failed_chapters += 1
            record_failure(section_state, dest_rel, f"book chapter '{title}' fetch failed: {reason}")
        parts.append(f"\n## {title}\n\n{body}\n")
    content = "".join(parts)

    if failed_chapters and dest_path.exists():
        # Do not replace a complete earlier capture with one that has holes in it.
        print(f"  [keep] {dest_rel} (kept the previous copy; {failed_chapters} chapter(s) failed)")
        _row(section_state["rows"], mod_name, "book", filename, "(root)")
        return 0

    written = _write_text_if_changed(dest_path, content)
    print(f"  [book] {dest_rel} ({len(chapters)} chapter(s))" if written
          else f"  [ok]   {dest_rel} (book unchanged)")
    _row(section_state["rows"], mod_name, "book", filename, "(root)")
    return 1 if written else 0


def handle_contents_module(
    base_url: str,
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

        # For page modules, index.html IS the page content (e.g. assignment
        # specifications). Render it to Markdown instead of skipping it.
        if modname == "page" and filename.lower() == "index.html":
            count += save_page_markdown(
                base_url, token, mod, fileurl, section_dir, dry_run, output_dir, section_state,
            )
            continue

        outcome = _place_file(
            base_url, token, fileurl, filename, mod_name, modname, section_dir, dry_run,
            downloaded_urls, output_dir, hash_index, section_state,
            misc_patterns, skip_patterns,
            remote_size=fc.get("filesize"),
            remote_mtime=fc.get("timemodified"),
        )
        if outcome == "downloaded":
            count += 1

    return count


def handle_label_module(
    base_url: str,
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
        if is_moodle_file_url(link, base_url):
            raw_filename = unquote(Path(urlparse(link).path).name)
            filename = sanitize_filename(raw_filename)
            if not filename:
                continue

            outcome = _place_file(
                base_url, token, link, filename, mod_name, "label", section_dir, dry_run,
                downloaded_urls, output_dir, hash_index, section_state,
                misc_patterns, skip_patterns,
                misc_reason="acknowledgement label" if is_misc_label else None,
            )
            if outcome == "downloaded":
                count += 1
        else:
            if link.startswith("http"):
                external_links.append((display, link))

    return count


def handle_url_module(
    base_url: str,
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

    if is_moodle_file_url(fileurl, base_url):
        raw_filename = unquote(Path(urlparse(fileurl).path).name)
        filename = sanitize_filename(raw_filename)
        if not filename:
            return 0

        outcome = _place_file(
            base_url, token, fileurl, filename, mod_name, "url", section_dir, dry_run,
            downloaded_urls, output_dir, hash_index, section_state,
            misc_patterns, skip_patterns,
            remote_size=contents[0].get("filesize"),
            remote_mtime=contents[0].get("timemodified"),
        )
        return 1 if outcome == "downloaded" else 0

    if fileurl.startswith("http"):
        display = collapse_ws(mod_name) or fileurl
        section_state["external_links"].append((display, fileurl))
    return 0


def handle_description_module(mod: dict, section_state: dict) -> int:
    """
    Handle modname in (quiz, assign): no downloadable files, so capture the
    module's description as a ``_content.md`` block, or a stub line when the
    description is empty. Always returns 0 (nothing downloaded).
    """
    modname = mod.get("modname", "")
    mod_name = mod.get("name", "").strip()
    description = mod.get("description", "")
    md_body = html_to_markdown(description) if description else ""
    if not md_body:
        md_body = f"*{modname.title()}: {mod_name}*"
    heading = mod_name or f"({modname.title()})"
    section_state["content_blocks"].append((heading, modname, md_body))
    return 0


def handle_unsupported_module(mod: dict, section_state: dict) -> int:
    """
    Catch-all for module types without a handler (forum, glossary, wiki, ...).

    Moodle layouts vary by unit/term/teacher, so an unhandled type is a loud
    warning, never a silent drop. Its description text is kept as a fallback
    content block. Always returns 0 (nothing downloaded).
    """
    modname = mod.get("modname", "")
    mod_name = mod.get("name", "").strip() or f"({modname})"
    description = mod.get("description", "")
    md_body = html_to_markdown(description) if description else ""
    if md_body and not is_content_block_skippable(md_body):
        section_state["content_blocks"].append(
            (mod_name, f"{modname} (unsupported)", md_body)
        )
    section_state["skipped"].append(
        f"{mod_name} ({modname}) - UNSUPPORTED module type, content not fully captured"
    )
    print(f"  [warn] unsupported module type '{modname}': {mod_name} "
          f"- NOT captured, check it on Moodle")
    return 0


# ---------------------------------------------------------------------------
# Unit guide PDF download
# ---------------------------------------------------------------------------

def _is_unit_guide_url(url: str) -> bool:
    """Check if a URL points to a unit guide portal (e.g. unitguides.example.edu)."""
    try:
        host = urlparse(url).hostname or ""
        return "unitguide" in host.lower()
    except Exception:
        return False


def select_unit_offering(
    source_url: str,
    response_url: str,
    body: str,
    period: Optional[Tuple[str, str]] = None,
) -> Optional[str]:
    """
    Pick the unit offering ID from a unit-guide portal response. A
    ``/unit_offerings/<ID>`` in the redirect URL wins; otherwise the page's
    offering links are filtered by the course code in ``full_code`` and, when
    ``period`` (the ``unit_guide_period`` config: a ``from_shortname`` regex
    and a ``label`` regex template) is given, by the teaching period it
    captures. Returns None unless exactly one distinct offering remains.
    """
    direct = re.search(r'/unit_offerings/(\d+)', response_url)
    if direct:
        return direct.group(1)
    full_code = parse_qs(urlparse(source_url).query).get("full_code", [""])[0]
    label_re = None
    if period:
        number = re.search(period[0], full_code)
        if number:
            label_re = period[1].replace("{n}", re.escape(number.group(1)))
    candidates = []
    for offering, label in re.findall(
        r'<a\b[^>]*href=["\'][^"\']*/unit_offerings/(\d+)[^"\']*["\'][^>]*>(.*?)</a>',
        body, re.I | re.S,
    ):
        if label_re and not re.search(label_re, _strip_tags(label)):
            continue
        if full_code and full_code.split('_')[0] not in _strip_tags(label):
            continue
        candidates.append(offering)
    unique = set(candidates)
    return unique.pop() if len(unique) == 1 else None


# Parts of a server-rendered PDF that change on every render without the document
# changing: the trailer /ID pair (random on each render) and the info dates.
_PDF_VOLATILE = [
    re.compile(rb"/ID\s*\[\s*(?:<[0-9A-Fa-f\s]*>|\((?:\\.|[^\\)])*\))\s*"
               rb"(?:<[0-9A-Fa-f\s]*>|\((?:\\.|[^\\)])*\))\s*\]"),
    re.compile(rb"/CreationDate\s*(?:\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>)"),
    re.compile(rb"/ModDate\s*(?:\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>)"),
]


def pdf_fingerprint(body: bytes) -> str:
    """
    SHA-256 of a PDF with its volatile metadata masked out (/ID, /CreationDate,
    /ModDate). Two renders of the same unit guide differ only there, so equal
    fingerprints mean "unchanged" even though the bytes differ.
    """
    for pattern in _PDF_VOLATILE:
        body = pattern.sub(b"", body)
    return hashlib.sha256(body).hexdigest()


def handle_unit_guide(
    fileurl: str,
    course_dir: Path,
    dry_run: bool,
    output_dir: Path,
    hash_index: dict,
    downloaded_urls: Set[str],
    period: Optional[Tuple[str, str]] = None,
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

    if dry_run:
        print(f"  [dry]  {dest_rel}")
        return False

    try:
        r = requests.get(fileurl, timeout=30, allow_redirects=True)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] Could not fetch unit guide page: {describe_error(e)}")
        return False

    offering_id = select_unit_offering(fileurl, r.url, r.text, period)
    if not offering_id:
        print(f"  [warn] Could not uniquely match the requested unit offering at {fileurl}")
        return False

    parsed = urlparse(r.url)
    pdf_url = f"{parsed.scheme}://{parsed.hostname}/unit_offerings/{offering_id}/unit_guide/print.pdf"

    try:
        r = requests.get(pdf_url, timeout=120)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] Could not download unit guide PDF: {describe_error(e)}")
        return False

    content_type = r.headers.get("Content-Type", "")
    if "pdf" not in content_type.lower():
        print(f"  [warn] Unit guide response was not a PDF (got {content_type})")
        return False

    body = r.content
    sha256 = hashlib.sha256(body).hexdigest()
    size = len(body)

    replacing = dest_path.exists()
    if replacing:
        # The portal re-renders the PDF on every request with a fresh /ID, so the
        # bytes never match. Compare with the volatile metadata masked; a real
        # edit by the unit convenor still changes the fingerprint.
        local = dest_path.read_bytes()
        if pdf_fingerprint(local) == pdf_fingerprint(body):
            local_sha = hashlib.sha256(local).hexdigest()
            print(f"  [skip] {dest_rel} (unchanged)")
            # The index must describe what is on disk, not the bytes just fetched.
            forget_hash_path(hash_index, dest_rel, local_sha)
            record_hash_path(hash_index, local_sha, len(local), dest_rel)
            return False

    print(f"  [upd]  {dest_rel}  (unit guide changed)" if replacing else f"  [dl]   {dest_rel}")
    _write_bytes(dest_path, body)
    forget_hash_path(hash_index, dest_rel, sha256)
    record_hash_path(hash_index, sha256, size, dest_rel)
    return True


# ---------------------------------------------------------------------------
# Section-level output: _links.md and _index.md
# ---------------------------------------------------------------------------

_LINKS_HEADER = "# External links - "
_LINKS_LINE_RE = re.compile(r"^- \[.*\]\(.*\)$")


def _remove_stale_links_file(links_path: Path, dry_run: bool, output_dir: Path) -> None:
    """
    Delete a _links.md this tool wrote earlier once its section has no external
    links, so sway_sync (which discovers decks from these files) stops seeing
    the old ones. Only a file that is exactly in the generated format goes; a
    hand-written or edited file is left alone.
    """
    try:
        lines = links_path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return
    if not lines or not lines[0].startswith(_LINKS_HEADER):
        return
    if not all(_LINKS_LINE_RE.match(line) for line in lines[1:] if line.strip()):
        return
    rel = _rel_for_display(links_path, output_dir)
    if dry_run:
        print(f"  [dry]  {rel} (would remove: no external links any more)")
        return
    links_path.unlink()
    print(f"  [links] {rel} removed (no external links any more)")


def write_links_file(
    section_dir: Path,
    week_label: str,
    external_links: List[Tuple[str, str]],
    dry_run: bool,
    output_dir: Path,
    keep_stale: bool = False,
    written: Optional[Set[Path]] = None,
    stale: Optional[List[Path]] = None,
):
    """
    Write external links collected for a section to _links.md. When there are
    none, a _links.md this tool generated in an earlier run is stale: it is
    removed at once, or, when ``stale`` is given, appended to that list so the
    caller can remove it after every section is done (several sections can share
    a folder, and one of them may have just written the file). ``keep_stale`` is
    set when a module failed and the link list may be incomplete. ``written``
    collects the paths this run has written.

    The ``- [title](url)`` line format is read back by sway_sync.py
    (SWAY_LINK_RE) to find Sway lecture decks; change one, change the other.
    """
    links_path = section_dir / "_links.md"
    written = written if written is not None else set()

    if not external_links:
        if not keep_stale:
            if stale is None:
                _remove_stale_links_file(links_path, dry_run, output_dir)
            else:
                stale.append(links_path)  # decided once every section has had its say
        return
    written.add(links_path)

    lines = [f"# External links - {week_label}", ""]
    for name, url in external_links:
        display = name if name else url
        lines.append(f"- [{display}]({url})")
    lines.append("")
    # Only our own token is stripped: a foreign "token=" parameter is part of the link.
    content = strip_secrets("\n".join(lines))

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
    name and the short course code, e.g. ``# Assessments - UNIT1000``.
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
    content = strip_secrets("\n".join(lines))

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
            lines.append(f"- {redact(item)}")
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
    content = strip_secrets("\n".join(lines))

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

def new_section_state(name_claims: Optional[dict] = None,
                      links_written: Optional[set] = None) -> dict:
    """
    Fresh per-section accumulator that module handlers append to.

    ``name_claims`` (the registry behind claim_name) and ``links_written`` (the
    _links.md paths written or kept by this run) are course-wide: several
    sections can map to one folder, so they share both.
    """
    return {
        "rows": [],
        "misc_rows": [],
        "external_links": [],
        "skipped": [],
        "content_blocks": [],
        "failures": [],
        "incomplete": False,
        "name_claims": name_claims if name_claims is not None else {},
        "links_written": links_written if links_written is not None else set(),
    }


def dispatch_module(
    base_url: str,
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
    Route one module to its handler by ``modname``. Returns the handler's count
    of files downloaded. Shared by sync_course and sync_one.py.
    """
    modname = mod.get("modname", "")

    if modname in ("resource", "folder", "page"):
        return handle_contents_module(
            base_url, token, mod, section_dir, dry_run, downloaded_urls,
            output_dir, hash_index, section_state,
            misc_patterns, skip_patterns,
        )
    if modname == "label":
        return handle_label_module(
            base_url, token, mod, section_dir, dry_run, downloaded_urls,
            output_dir, hash_index, section_state,
            misc_patterns, skip_patterns,
        )
    if modname == "url":
        return handle_url_module(
            base_url, token, mod, section_dir, dry_run, downloaded_urls,
            output_dir, hash_index, section_state,
            misc_patterns, skip_patterns,
        )
    if modname == "book":
        return handle_book_module(
            base_url, token, mod, section_dir, dry_run, output_dir, section_state,
        )
    if modname in ("quiz", "assign"):
        return handle_description_module(mod, section_state)
    return handle_unsupported_module(mod, section_state)


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
    unit_guide_period: Optional[Tuple[str, str]] = None,
) -> int:
    """
    Sync one course. A failure never stops the rest of the course: a file or
    module that raises is logged as ``[fail]``, listed in its section's
    _index.md, and the section's index files are still written. Returns the
    number of failures (0 when everything worked).
    """
    print(f"\n{'='*60}")
    print(f"Course: {course['shortname']} - {course['fullname']}")
    print(f"Folder: {_rel_for_display(course_dir, output_dir)}")
    print("="*60)

    try:
        sections = get_contents(base_url, token, course["id"])
    except Exception as e:
        print(f"  [fail] {course['shortname']}: could not read the course contents: "
              f"{describe_error(e)}")
        return 1
    failures = 0
    downloaded = 0
    downloaded_urls: Set[str] = set()
    name_claims: dict = {}
    links_written: Set[Path] = set()
    stale_links: List[Path] = []
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
                try:
                    if handle_unit_guide(
                        fileurl, course_dir, dry_run, output_dir,
                        hash_index, downloaded_urls, unit_guide_period,
                    ):
                        downloaded += 1
                except Exception as e:
                    print(f"  [fail] {_rel_for_display(course_dir / 'Unit_Guide.pdf', output_dir)}: "
                          f"{describe_error(e)}")
                    failures += 1

    for section_index, section in enumerate(sections):
        sec_name = clean_section_name(section.get("name", ""))
        modules = section.get("modules", [])
        if not modules and not (section.get("summary") or "").strip():
            continue

        section_dir = section_dir_for(course_dir, sec_name, section_index)
        week_label = section_dir.name

        print(f"\n  Section: {sec_name or week_label}")

        section_state = new_section_state(name_claims, links_written)

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
            try:
                downloaded += dispatch_module(
                    base_url, token, mod, section_dir, dry_run, downloaded_urls,
                    output_dir, hash_index, section_state, misc_patterns, skip_patterns,
                )
            except Exception as e:
                # Last line of defence; file-level failures are already caught in
                # _place_file. The module may have collected only part of its
                # links, so the section's link list is not complete.
                section_state["incomplete"] = True
                label = f"{collapse_ws(mod.get('name', '')) or '(unnamed)'} ({mod.get('modname', '?')})"
                record_failure(section_state, label, describe_error(e))

        try:
            write_links_file(section_dir, week_label, section_state["external_links"],
                             dry_run, output_dir, keep_stale=section_state["incomplete"],
                             written=section_state["links_written"], stale=stale_links)
            wrote_content = write_section_content(
                section_dir, sec_name or week_label, course_code,
                section_state["content_blocks"], dry_run, output_dir,
            )
            write_section_index(
                section_dir, week_label, section_state, dry_run, output_dir,
                has_content_md=wrote_content,
            )
        except Exception as e:
            record_failure(section_state, f"{week_label}/_index.md etc.", describe_error(e))
        failures += len(section_state["failures"])

    for links_path in stale_links:
        if links_path not in links_written:
            _remove_stale_links_file(links_path, dry_run, output_dir)

    print(f"\n  Total downloaded: {downloaded} file(s)")
    return failures


# ---------------------------------------------------------------------------
# Config and token loading
# ---------------------------------------------------------------------------

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


def parse_unit_guide_period(cfg: dict) -> Optional[Tuple[str, str]]:
    """
    Parse the optional ``unit_guide_period`` config: a mapping with a
    ``from_shortname`` regex (group 1 captures the teaching-period number in
    the course shortname) and a ``label`` regex in which ``{n}`` stands for
    that number. Returns (from_shortname, label), or None when the key is
    missing or invalid (invalid values are warned about on stderr).
    """
    raw = cfg.get("unit_guide_period")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        print("Config warning: 'unit_guide_period' must be a mapping with "
              "'from_shortname' and 'label'; ignoring.", file=sys.stderr)
        return None
    from_shortname, label = raw.get("from_shortname"), raw.get("label")
    if not (isinstance(from_shortname, str) and from_shortname
            and isinstance(label, str) and label):
        print("Config warning: 'unit_guide_period' needs non-empty 'from_shortname' "
              "and 'label' strings; ignoring.", file=sys.stderr)
        return None
    try:
        if re.compile(from_shortname).groups < 1:
            raise re.error("needs a capturing group for the period number")
        re.compile(label.replace("{n}", "0"))
    except re.error as e:
        print(f"Config warning: 'unit_guide_period' has an invalid regex ({e}); ignoring.",
              file=sys.stderr)
        return None
    return from_shortname, label


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


# ---------------------------------------------------------------------------
# Interactive setup wizard (--setup)
# ---------------------------------------------------------------------------

def run_setup():
    """Interactive setup wizard for first-time configuration."""
    # ---- Step 1: Welcome ----
    print()
    print("=" * 60)
    print("  course-sync setup wizard")
    print("=" * 60)
    print()
    print("This tool downloads and organises your course files from")
    print("Moodle into neat local folders.")
    print()
    print("You will need:")
    print("  1. Your Moodle site URL")
    print("  2. A web service token (from your Moodle profile)")
    print()

    config_path = REPO_DIR / "config.yaml"
    token_path = REPO_DIR / ".course_sync_token"

    # ---- Steps 2-4: Moodle URL + Token + Connection test (with retry) ----
    base_url = ""
    token = ""
    user_id = None
    fullname = ""

    while True:
        # Step 2: Moodle URL
        print("Step 1: Moodle URL")
        print("-" * 40)
        url_input = input("Enter your Moodle URL (e.g. https://moodle.example.edu): ").strip()
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
            print(f"  Connection failed: {describe_error(e)}")
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
            print("  python3 src/course_sync.py --dry-run")
            print()
            print("To sync your files:")
            print("  python3 src/course_sync.py")
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
    print("  python3 src/course_sync.py --dry-run")
    print()
    print("To sync your files:")
    print("  python3 src/course_sync.py")
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def run_guarded(func, *args):
    """
    Call ``func(*args)`` so that an uncaught exception ends the run with a one-line,
    redacted message on stderr and exit status 1, never a raw traceback (tracebacks
    quote the requests exception text, which carries the token). Set
    COURSE_SYNC_DEBUG=1 to also get the traceback, redacted. SystemExit passes through.
    """
    try:
        return func(*args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Error: {describe_error(e)}", file=sys.stderr)
        if os.environ.get("COURSE_SYNC_DEBUG"):
            print(redact(traceback.format_exc()), file=sys.stderr)
        sys.exit(1)


def main():
    run_guarded(_main)


def _main():
    parser = argparse.ArgumentParser(description="Sync course files to local folders")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help="Path to config YAML (default: config.yaml in the repo root)")
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
        print(f"Error: Could not authenticate. Check your token.\n{redact(e)}")
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

    hash_index = load_hash_index(output_dir, move_aside=not args.dry_run)

    misc_patterns = parse_pattern_list(cfg, "misc_patterns")
    skip_patterns = parse_pattern_list(cfg, "skip_patterns")
    unit_guide_period = parse_unit_guide_period(cfg)

    failures = 0
    for course, folder_name in matched:
        course_dir = output_dir / folder_name
        try:
            failures += sync_course(
                base_url, token, course, course_dir, args.dry_run,
                output_dir, hash_index, misc_patterns, skip_patterns,
                unit_guide_period,
            )
        except Exception as e:
            print(f"  [fail] {course['shortname']}: {describe_error(e)}")
            failures += 1
        finally:
            # Save even after a failure: files fetched before it are on disk and
            # must stay known to the dedup index.
            if not args.dry_run:
                save_hash_index(output_dir, hash_index)

    if failures:
        print(f"\nDone, with {failures} failure(s). See the [fail] lines above.")
        sys.exit(1)
    print("\nDone.")


if __name__ == "__main__":
    main()

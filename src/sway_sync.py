#!/usr/bin/env python3
"""
sway_sync - export Microsoft Sway lecture decks to local markdown files.

Sway decks are web-only and sit behind the university's Microsoft 365 login,
so course-sync (Moodle API) can never download them. This tool drives its own
headless Chromium via Playwright, using a persistent browser profile that you
log into ONCE:

    .venv-sway/bin/python src/sway_sync.py --login     # opens a window, sign in, close it
    .venv-sway/bin/python src/sway_sync.py             # export all decks headlessly
    .venv-sway/bin/python src/sway_sync.py --check     # report new/changed decks, write nothing

Deck URLs are discovered from the _links.md files that course-sync writes
(links whose URL is on sway.cloud.microsoft). The units to scan and the output
root come from config.yaml (courses[].folder and output_dir), the same file
course-sync uses. Output goes to
<UNIT>/Week<N>/<link title>.md, beside the lesson PDFs, with the week taken
from the deck title's W<week>L<lesson> prefix. Files are only rewritten when
the deck text actually changed (content hash kept in .sway-state.json in the repo root).

Embedded images (graph-paper sketches, memory diagrams, operator tables) are
downloaded into <deck name>_images/ beside the markdown and referenced inline,
placed after the deck text that immediately precedes them. Use --no-images for
the old text-only behaviour.

Exit codes: 0 ok · 1 error · 2 login required (session expired) ·
3 --check found new/changed decks.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

# This file lives in <repo>/src/; the repo root is the runtime root (config,
# session, browser profile, state and the .venv-sway interpreter all sit there).
REPO_DIR = Path(__file__).resolve().parents[1]

# Re-exec under the tool's own venv so `python3 src/sway_sync.py` just works no
# matter which interpreter invoked it (system pythons have the wrong or no
# playwright + browsers).
_VENV_PY = REPO_DIR / ".venv-sway" / "bin" / "python"
if _VENV_PY.exists() and Path(sys.executable).resolve() != _VENV_PY.resolve():
    os.execv(str(_VENV_PY), [str(_VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Runtime files live in the repo root (not beside this script), never in the content
# tree: the Microsoft session, the browser profile, the per-deck content hashes.
CONFIG_FILE = REPO_DIR / "config.yaml"
AUTH_FILE = REPO_DIR / ".sway-auth.json"
PROFILE_DIR = REPO_DIR / ".sway-profile"
STATE_FILE = REPO_DIR / ".sway-state.json"

# Used when config.yaml is missing or is not in a shape read_layout() understands:
# with no unit folders configured there is nothing to scan.
FALLBACK_UNIT_DIRS: list = []


def _scalar(text):
    """Value of a one-line YAML scalar: trailing comment and quotes removed."""
    text = text.strip()
    if text.startswith("#"):
        return ""
    if text[:1] in ("'", '"'):
        end = text.find(text[0], 1)
        if end == -1 or "\\" in text[:end]:
            raise ValueError(f"unsupported quoting: {text!r}")
        return text[1:end]
    if text[:1] in ("[", "{", "|", ">", "&", "*", "!"):
        raise ValueError(f"unsupported YAML value: {text!r}")
    text = re.sub(r"\s+#.*$", "", text).strip()
    return "" if text in ("~", "null", "Null", "NULL") else text


def read_layout(text):
    """Pull output_dir and the courses' folder names out of config.yaml text.

    course-sync owns config.yaml and reads it with PyYAML, but this tool's
    venv does not have PyYAML (and cannot import course_sync, which needs
    requests, PyYAML and markdownify). So this understands only the two shapes
    the config uses:

        output_dir: ..
        courses:
          - code: UNIT1000_T2_2026_ALL
            folder: UNIT1000

    Returns (output_dir or None, [folder, ...]); folder defaults to code, as
    in course_sync.parse_courses. Raises ValueError on anything else (flow
    style, anchors, multi-line values) so the caller falls back rather than
    guesses.
    """
    output_dir = None
    courses = []
    in_courses = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = re.match(r"([A-Za-z_][\w-]*):(?:\s+(.*))?$", line)
        if top:  # an unindented "key: value" line ends any block
            key, value = top.group(1), top.group(2) or ""
            in_courses = key == "courses"
            if in_courses and _scalar(value):
                raise ValueError("courses must be a block list")
            if key == "output_dir":
                output_dir = _scalar(value) or None
            continue
        if in_courses:
            item = re.match(r"\s*-\s+([A-Za-z_][\w-]*):(?:\s+(.*))?$", line)
            field = re.match(r"\s+([A-Za-z_][\w-]*):(?:\s+(.*))?$", line)
            if item:
                courses.append({})
                match = item
            elif field and courses:
                match = field
            else:
                raise ValueError(f"unsupported courses line: {line.strip()!r}")
            courses[-1][match.group(1)] = _scalar(match.group(2) or "")
        elif not line.startswith((" ", "\t", "- ")) and line.strip() != "---":
            raise ValueError(f"unsupported line: {line.strip()!r}")
    folders = []
    for course in courses:
        if not course.get("code"):
            raise ValueError("a courses entry has no code")
        folders.append(course.get("folder") or course["code"])
    return output_dir, folders


def load_layout(config_path=CONFIG_FILE):
    """Return (base_dir, unit_dirs): the output tree root and its unit folders.

    Both come from config.yaml (output_dir resolved against the config's own
    folder, like course_sync.resolve_path; courses[].folder in config order),
    so adding a unit to the config is enough. With no config, or one that
    read_layout() cannot read, fall back to the folder above this repo and
    FALLBACK_UNIT_DIRS (no units).
    """
    base_dir, unit_dirs = REPO_DIR.parent, list(FALLBACK_UNIT_DIRS)
    if not config_path.exists():
        return base_dir, unit_dirs
    try:
        output_dir, folders = read_layout(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"sway_sync: could not read units from {config_path.name} ({e}); "
              f"no unit folders will be scanned", file=sys.stderr)
        return base_dir, unit_dirs
    if output_dir:
        path = Path(output_dir).expanduser()
        base_dir = path if path.is_absolute() else (config_path.parent / path).resolve()
    if folders:
        unit_dirs = list(dict.fromkeys(folders))
    return base_dir, unit_dirs


BASE_DIR, UNIT_DIRS = load_layout()
# Decks land beside the lesson PDFs in the unit's week folder, e.g.
# UNIT1000/Week3/W3L2 - Example deck.md.
OUT_DIR = BASE_DIR

# Deck titles are prefixed W<week>L<lesson>, e.g. "W3L2 - Example deck".
WEEK_PREFIX_RE = re.compile(r"^W(\d+)L\d+")
# course-sync writes external links to _links.md as "- [title](url)" lines
# (course_sync.write_links_file). This is the only coupling between the tools.
SWAY_LINK_RE = re.compile(r"\[([^\]]+)\]\((https://sway\.cloud\.microsoft/[A-Za-z0-9]+)\)")
FOOTER_MARKER = "Made with Microsoft Sway"
LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com")
# Sway shows this interstitial on its own domain when unauthenticated, so
# checking for redirects to the login hosts is not enough.
SIGNIN_MARKERS = ("Sign-in is required", "Sign in with a work or school account")

# Deck-hosted images live at /s/<deckId>/images/<imageId>; everything else an
# <img> points at (eus-cdn.sway.static.microsoft) is Sway's own UI chrome -
# spinners, layout-picker icons, the theme's decorative story.png.
DECK_IMAGE_RE = re.compile(r"/s/[A-Za-z0-9]+/images/([A-Za-z0-9_-]+)")
# Files this tool writes into <deck>_images/. Only these are ever deleted.
TOOL_IMAGE_RE = re.compile(r"^\d{2}-[A-Za-z0-9_-]+\.[A-Za-z0-9]+$")
# `quality` caps the longest edge; Sway returns the original when it is smaller.
IMAGE_QUALITY = 4096
CONTENT_TYPE_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
    "image/bmp": ".bmp", "image/tiff": ".tif",
}

# Walk the DOM in document order, remembering the last leaf element that had
# visible text, and tag every deck image with it. That "anchor" is what the
# deck shows immediately above the image, so it is where the image belongs in
# the flattened text. Sway's alt attribute holds its emphasis setting
# ("(Moderate)", "(Less important)"), never a caption, so it is not collected.
# Sway scrolls an inner div (#storyroot), NOT the document. document.scrollingElement
# reports scrollHeight == clientHeight, so assigning to its scrollTop silently does
# nothing and the deck never advances: on W1L4 that left 2 of 5 images permanently
# unloaded while the text looked complete, because Sway renders all text up front and
# lazy-loads only images. Resolve the real container, and fall back to the deepest
# scrollable element if Microsoft renames the id.
SCROLL_JS = r"""
(dy) => {
  const el = document.getElementById('storyroot')
    || [...document.querySelectorAll('*')].find(
         e => e.scrollHeight > e.clientHeight + 50 && e.clientHeight > 200)
    || document.scrollingElement;
  if (!el) return null;
  if (dy) el.scrollTop += dy;
  return {top: Math.round(el.scrollTop), view: Math.round(el.clientHeight),
          full: Math.round(el.scrollHeight)};
}
"""

HARVEST_JS = r"""
() => {
  const deckImg = /\/s\/[A-Za-z0-9]+\/images\/([A-Za-z0-9_-]+)/;
  const out = [];
  let anchor = '';
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let n = w.currentNode;
  while (n) {
    if (n.tagName === 'IMG') {
      const src = n.currentSrc || n.src || '';
      const m = src.match(deckImg);
      if (m) out.push({id: m[1], src: src.split('?')[0], anchor: anchor,
                       inToC: !!n.closest('[class*="ToCView"]')});
    } else if (n.children.length === 0) {
      const t = (n.innerText || '').trim();
      if (t) anchor = t.slice(0, 160);
    }
    n = w.nextNode();
  }
  return out;
}
"""

# One deck can be linked from several weeks; key by URL and keep first title seen.


def discover_decks():
    """Scan course-sync's _links.md files for Sway URLs. Returns {url: (unit, title)}."""
    decks = {}
    for unit in UNIT_DIRS:
        for links_md in sorted((BASE_DIR / unit).rglob("_links.md")):
            text = links_md.read_text(encoding="utf-8", errors="replace")
            for title, url in SWAY_LINK_RE.findall(text):
                decks.setdefault(url, (unit, title.strip()))
    return decks


def deck_dest(unit, title):
    """Destination path for a deck: <unit>/Week<N>/<title>.md.

    Falls back to the unit root when the title carries no W<week>L<lesson>
    prefix, so an unrecognised deck is still written somewhere obvious rather
    than silently dropped.
    """
    m = WEEK_PREFIX_RE.match(title)
    if not m:
        return OUT_DIR / unit / safe_filename(title)
    week = int(m.group(1))
    unit_dir = OUT_DIR / unit
    # course-sync names a study week's folder Week5_StudyWeek. Prefer whatever folder
    # course-sync actually created over guessing a name.
    candidates = [f"Week{week}"] if week != 5 else [f"Week{week}_StudyWeek", f"Week{week}"]
    for name in candidates:
        if (unit_dir / name).is_dir():
            return unit_dir / name / safe_filename(title)
    return unit_dir / candidates[0] / safe_filename(title)


def safe_filename(title):
    name = re.sub(r'[/\\:*?"<>|]', "-", title).strip().rstrip(".")
    return (name or "untitled")[:150] + ".md"


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"decks": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def clean_deck_text(raw):
    """Strip Sway UI chrome from extracted page text."""
    lines = raw.splitlines()
    # Drop leading chrome ("Accessibility View", "Share", blanks) before the title.
    while lines and lines[0].strip() in ("", "Accessibility View", "Share"):
        lines.pop(0)
    # Cut the promo footer.
    for i, line in enumerate(lines):
        if FOOTER_MARKER in line:
            lines = lines[:i]
            break
    return "\n".join(lines).strip()


def download_images(ctx, images, img_dir):
    """
    Fetch each deck image through the authenticated context and write it into
    img_dir. Returns the records that landed, each gaining a "file" name. A
    failed image is reported and skipped rather than failing the whole deck.

    Stale files from a previous export of the SAME deck are then removed, but
    only ones matching this function's own naming pattern (NN-<swayImageId>.ext)
    inside this deck's own _images/ directory. Without that, re-ordering a deck
    renumbers every file and leaves a duplicate copy of each image behind under
    its old number: one re-run produced 47 such duplicates. Nothing outside the
    tool's own output is ever touched, and a hand-added file in that folder does
    not match the pattern and so survives.
    """
    ok = []
    for i, rec in enumerate(images, 1):
        url = f"{rec['src']}?quality={IMAGE_QUALITY}&allowAnimation=true"
        try:
            resp = ctx.request.get(url, timeout=30000)
            if not resp.ok:
                print(f"    [img] HTTP {resp.status} for {rec['id']}", file=sys.stderr)
                continue
            body = resp.body()
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        except Exception as e:
            print(f"    [img] {rec['id']}: {e}", file=sys.stderr)
            continue
        if len(body) < 100:
            print(f"    [img] {rec['id']}: empty response", file=sys.stderr)
            continue
        ext = CONTENT_TYPE_EXT.get(ctype, ".png")
        rec["file"] = f"{i:02d}-{rec['id']}{ext}"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / rec["file"]).write_bytes(body)
        ok.append(rec)

    # Only prune when EVERY image downloaded. A partial run means `keep` is
    # missing files that are still perfectly good on disk, and pruning against
    # it deletes them. A transient DNS failure mid-run did exactly that: all of
    # W2L4's downloads failed, keep was empty, and its 5 existing images were
    # deleted. Losing data to a network blip is far worse than leaving stale
    # files around for the next successful run to clear.
    if ok and len(ok) == len(images):
        keep = {r["file"] for r in ok}
        stale = [f for f in img_dir.glob("*")
                 if f.is_file() and f.name not in keep and TOOL_IMAGE_RE.match(f.name)]
        for f in stale:
            f.unlink()
        if stale:
            print(f"    [img] removed {len(stale)} stale file(s) from a previous export")
    elif images:
        print(f"    [img] {len(images) - len(ok)} of {len(images)} failed; "
              f"keeping existing files untouched", file=sys.stderr)
    return ok


def _norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def order_images(text, images):
    """
    Sort images by where their anchor first appears in the deck text.

    Sighting order is not deck order. Sway's table-of-contents strip preloads
    some section thumbnails before the scroll pass reaches the body, so on W1L4
    the two "simple picture" images were seen before sketches (b) and (c) even
    though they come after them. Placing in sighting order then walked the
    cursor past (b) and (c) and dumped both in the unplaced section.

    Anchors that match nothing keep their relative order and go last.
    """
    norm_lines = [_norm(l) for l in text.split("\n")]

    def first_line(rec):
        anchor = _norm((rec.get("anchor") or "").split("\n")[0])
        if len(anchor) < 3:
            return len(norm_lines) + 1
        for j, ln in enumerate(norm_lines):
            if ln and anchor in ln:
                return j
        return len(norm_lines) + 1

    return [r for _, _, r in sorted(
        ((first_line(r), i, r) for i, r in enumerate(images)), key=lambda t: (t[0], t[1]))]


def place_images(text, images, img_dir_name):
    """
    Splice image references into the deck text at their anchors.

    Each image carries the text that sat immediately above it in the DOM, so
    the reference goes on the line after that text. Matching only ever moves
    forward, which keeps the images in deck order and stops a repeated anchor
    like "In-class Activity" from pulling a later image back up the document.
    Anything that cannot be matched is listed at the end rather than dropped or
    guessed at.
    """
    lines = text.split("\n")

    # Sway's extracted text wraps mid-sentence, so an anchor like "Writing a
    # nested loop to generate a numerical pattern" can straddle two lines and be
    # invisible to a per-line search. Keep a whitespace-flattened copy of the
    # whole deck alongside a map back to line numbers, and fall back to it.
    spans = []          # (start, end, line index) into `flat`
    parts, pos = [], 0
    for j, line in enumerate(lines):
        nl = _norm(line)
        if not nl:
            continue
        if parts:
            parts.append(" ")
            pos += 1
        spans.append((pos, pos + len(nl), j))
        parts.append(nl)
        pos += len(nl)
    flat = "".join(parts)

    def find_flat(anchor, cursor):
        """Line index holding the end of `anchor`, searching at/after `cursor`."""
        start = next((s for s, _, j in spans if j >= cursor), None)
        if start is None:
            return None
        at = flat.find(anchor, start)
        if at < 0:
            return None
        last = at + len(anchor) - 1
        for s, e, j in spans:
            if s <= last < e:
                return j
        return None

    def locate(anchor, from_line):
        """First line at/after from_line containing the anchor.

        This must use the same rule as order_images, or the two disagree and
        the cursor desynchronises. An earlier version preferred an exact
        whole-line match anywhere ahead over a substring match nearby: on W1L3
        the one-word anchor "colors" matched a bare "colors" line 200 lines
        further down, dragging the cursor past everything and orphaning the
        following 14 images.
        """
        for j in range(from_line, len(lines)):
            ln = _norm(lines[j])
            if ln and anchor in ln:
                return j
        return find_flat(anchor, from_line)

    after = {}          # line index -> [markdown]
    unplaced = []
    cursor, prev_idx, prev_anchor = 0, None, None
    for rec in images:
        anchor = _norm((rec.get("anchor") or "").split("\n")[0])
        idx = None
        if len(anchor) >= 3:
            if anchor == prev_anchor and prev_idx is not None:
                # Consecutive images under one heading: stack them there rather
                # than letting the second claim a later occurrence of the same
                # words. On W1L3 the second "Recommended videos" image jumped to
                # a repeat of that heading 12 lines on, and the three images
                # anchored in between were orphaned by the advancing cursor.
                idx = prev_idx
            else:
                idx = locate(anchor, cursor)
            # A deck can put two images under one instruction, in which case the
            # phrase occurs once in the text and the first image already moved
            # the cursor past it. Retry from the previous image's line so the
            # second stacks beneath the same anchor instead of being orphaned.
            if idx is None and prev_idx is not None:
                idx = locate(anchor, prev_idx)
        if idx is None:
            unplaced.append(rec)
            continue
        after.setdefault(idx, []).append(md_image(rec, img_dir_name))
        cursor, prev_idx, prev_anchor = idx + 1, idx, anchor

    out = []
    for j, ln in enumerate(lines):
        out.append(ln)
        for md in after.get(j, []):
            out.append("")
            out.append(md)
    body = "\n".join(out)

    if unplaced:
        body += (
            "\n\n## Deck images that could not be placed\n\n"
            "These are from the deck but their position in the text above could not be\n"
            "determined, so they are listed here in deck order rather than guessed at.\n"
        )
        for rec in unplaced:
            body += "\n" + md_image(rec, img_dir_name) + "\n"
    return body


def md_image(rec, img_dir_name):
    alt = re.sub(r"\s+", " ", (rec.get("anchor") or "").strip()) or "Sway deck image"
    path = f"{quote(img_dir_name)}/{quote(rec['file'])}"
    return f"![{alt.replace('[', '(').replace(']', ')')}]({path})"


def extract_deck(page, url, want_images=True):
    """
    Load a Sway deck and return (full visible text, ordered image records).
    Raises LoginRequired if the session has expired.

    Sway virtualises: an <img> only has a src while it is near the scroll
    offset, so a single snapshot at the end of the pass sees a fraction of the
    deck's images. Images are therefore harvested at every scroll step and
    accumulated, keyed by Sway's image id. First sighting wins for ordering,
    which is deck order because the pass runs top to bottom. A sighting in the
    main flow supersedes one in the table-of-contents strip, since the flow
    carries the more accurate anchor.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3000)
    _check_signed_in(page, url)

    found = {}

    def harvest():
        if not want_images:
            return
        for rec in page.evaluate(HARVEST_JS):
            prev = found.get(rec["id"])
            if prev is None or (prev["inToC"] and not rec["inToC"]):
                # Keep the original slot so ordering reflects first sighting.
                rec["seq"] = prev["seq"] if prev else len(found)
                found[rec["id"]] = rec

    def result(text):
        return text, sorted(found.values(), key=lambda r: r["seq"])

    # Scroll to the true bottom of the deck, harvesting at every step. The step
    # is smaller than the viewport so no band goes past unseen, which would
    # leave an image unloaded and so uncaptured.
    #
    # Termination is by scroll position, NOT by text length going quiet. Sway
    # renders all of its text on load, so the text stabilises almost
    # immediately, long before the lower images have lazy-loaded; stopping on
    # that signal silently truncated every deck's images.
    text = ""
    at_bottom = 0
    for _ in range(120):  # generous: the longest deck here is ~6000px
        harvest()
        geo = page.evaluate(SCROLL_JS, 600)
        page.wait_for_timeout(700)
        _check_signed_in(page, url)
        text = page.evaluate("() => document.body.innerText")
        if geo and geo["full"] > 0 and geo["top"] + geo["view"] >= geo["full"] - 5:
            # Sit at the bottom for a few more passes so the last images finish.
            at_bottom += 1
            if at_bottom >= 4:
                break
        else:
            at_bottom = 0
    harvest()
    if FOOTER_MARKER in text or len(text) > 400:
        return result(text)
    raise RuntimeError(f"deck did not finish rendering: {url}")


class LoginRequired(Exception):
    pass


def _check_signed_in(page, url):
    if any(h in page.url for h in LOGIN_HOSTS):
        raise LoginRequired(url)
    text = page.evaluate("() => document.body.innerText") or ""
    if len(text) < 400 and any(m in text for m in SIGNIN_MARKERS):
        raise LoginRequired(url)


def run_login():
    """
    Open a headed browser on a deck URL, wait until the user has signed in
    (deck content becomes visible), then snapshot the session cookies to
    AUTH_FILE while the window is still open. Microsoft's auth cookies are
    session-scoped unless 'Stay signed in' is chosen, so waiting for the user
    to close the window and relying on the profile on disk loses them - the
    snapshot must happen while the context is alive.
    """
    decks = discover_decks()
    test_url = next(iter(decks), "https://sway.cloud.microsoft")
    print("A browser window will open. Sign in with your university Microsoft account")
    print("(choose 'Stay signed in: Yes' if asked - it makes the session last longer).")
    print("As soon as the deck content is visible I will save the session and close")
    print("the window automatically.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto(test_url)
        for i in range(600):  # up to 20 minutes to complete sign-in + MFA
            pg.wait_for_timeout(2000)
            try:
                if pg.is_closed():
                    # The login flow may have continued in another tab/popup.
                    open_pages = [q for q in ctx.pages if not q.is_closed()]
                    if not open_pages:
                        print("Window was closed before sign-in completed - nothing saved.",
                              file=sys.stderr)
                        return 1
                    pg = open_pages[0]
                text = pg.evaluate("() => document.body.innerText") or ""
            except Exception:
                continue  # mid-navigation
            if i % 15 == 14:  # progress line every ~30s so stalls are diagnosable
                print(f"  waiting... url={pg.url[:90]} title={pg.title()[:60]!r} "
                      f"textlen={len(text)}", flush=True)
            if "sway.cloud.microsoft" in pg.url and FOOTER_MARKER in text:
                ctx.storage_state(path=str(AUTH_FILE))
                AUTH_FILE.chmod(0o600)
                ctx.close()
                print(f"Signed-in session saved to {AUTH_FILE.name}. "
                      "Run without --login to export.")
                return 0
        ctx.close()
        print("Timed out waiting for sign-in.", file=sys.stderr)
        return 1


def run_sync(check_only=False, want_images=True, only=None, force=False):
    if not AUTH_FILE.exists():
        print("No saved session found. Run with --login first.", file=sys.stderr)
        return 2

    decks = discover_decks()
    if not decks:
        print("No Sway links found in the configured courses; nothing to sync.")
        return 0
    if only:
        decks = {u: v for u, v in decks.items() if only.lower() in v[1].lower()}
        if not decks:
            print(f"No deck title contains {only!r}.", file=sys.stderr)
            return 1
    print(f"Found {len(decks)} Sway deck link(s).")

    state = load_state()
    changed, new, failed, unchanged = [], [], [], []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=str(AUTH_FILE),
                                  viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()
        try:
            for url, (unit, title) in decks.items():
                try:
                    raw, images = extract_deck(page, url, want_images=want_images)
                    text = clean_deck_text(raw)
                except LoginRequired:
                    print("\nSession expired - run with --login to sign in again.", file=sys.stderr)
                    return 2
                except Exception as e:
                    print(f"  [fail] {title}: {e}", file=sys.stderr)
                    failed.append(title)
                    continue

                # Put the images in deck order before anything else, so the file
                # numbering and the placement pass both follow the text.
                images = order_images(text, images)
                # The hash covers the image ids as well as the text, so a deck
                # that swaps a diagram without touching a word is still caught.
                # Ids are enough: Sway mints a new one per upload, so an id
                # that persists points at the same bytes. Hashing the bytes
                # would mean downloading every image on every --check.
                img_ids = "\n".join(r["id"] for r in images)
                sha = hashlib.sha256(f"{text}\n\x00\n{img_ids}".encode("utf-8")).hexdigest()
                prev = state["decks"].get(url)
                dest = deck_dest(unit, title)
                img_dir = dest.parent / (dest.stem + "_images")
                # A deck is only "unchanged" if its markdown and every image it
                # recorded are still on disk AND nothing failed to download last
                # time. Without the `missing` check a permanently failing image
                # would be reported once and then silently skipped forever,
                # because the hash covers harvested ids, not downloaded files.
                have_imgs = all((img_dir / f).exists() for f in prev.get("images", [])) if prev else False
                if (prev and prev.get("sha") == sha and dest.exists() and have_imgs
                        and not prev.get("missing") and not force):
                    print(f"  [ok]   {title}")
                    unchanged.append(title)
                    continue

                bucket = changed if prev else new
                bucket.append(title)
                if check_only:
                    print(f"  [{'CHANGED' if prev else 'NEW'}] {title}")
                    continue

                dest.parent.mkdir(parents=True, exist_ok=True)
                saved = download_images(ctx, images, img_dir) if images else []
                if saved:
                    note = (f"> Note: text plus {len(saved)} deck image(s), downloaded into "
                            f"`{img_dir.name}/`. Images are placed under the deck text they\n"
                            f"> followed; Sway's own layout is richer, so open the source link "
                            f"if placement looks off.\n")
                elif want_images:
                    note = "> Note: this deck has no embedded images; the text below is complete.\n"
                else:
                    note = ("> Note: text-only export (--no-images). Embedded images/diagrams "
                            "are not captured.\n")
                header = (
                    f"# {title}\n\n"
                    f"> Source: {url}\n"
                    f"> Unit: {unit} · Exported from Sway {date.today().isoformat()} by sway_sync\n"
                    f"{note}\n"
                )
                body = place_images(text, saved, img_dir.name) if saved else text
                dest.write_text(header + body + "\n", encoding="utf-8")
                state["decks"][url] = {"sha": sha, "title": title, "unit": unit,
                                       "file": str(dest.relative_to(BASE_DIR)),
                                       "images": [r["file"] for r in saved],
                                       "missing": len(images) - len(saved),
                                       "last_export": date.today().isoformat()}
                save_state(state)  # save as we go so a crash keeps progress
                suffix = f" (+{len(saved)} image(s))" if saved else ""
                print(f"  [{'upd' if prev else 'new'}]  {title} -> "
                      f"{dest.relative_to(BASE_DIR)}{suffix}")
            # Re-snapshot the session: refreshed cookies extend its lifetime.
            ctx.storage_state(path=str(AUTH_FILE))
            AUTH_FILE.chmod(0o600)
        finally:
            ctx.close()
            browser.close()

    print(f"\nDone. {len(new)} new, {len(changed)} changed, {len(unchanged)} unchanged, "
          f"{len(failed)} failed.")
    if failed:
        return 1
    if check_only and (new or changed):
        return 3
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--login", action="store_true", help="open a window to sign in once")
    ap.add_argument("--check", action="store_true", help="report new/changed decks without writing")
    ap.add_argument("--no-images", action="store_true",
                    help="text-only export; do not download embedded images")
    ap.add_argument("--only", metavar="SUBSTR",
                    help="only decks whose title contains SUBSTR (case-insensitive)")
    ap.add_argument("--force", action="store_true",
                    help="re-export even when the deck is unchanged (use after "
                         "changing how the markdown is built)")
    args = ap.parse_args()
    if args.login:
        return run_login()
    return run_sync(check_only=args.check, want_images=not args.no_images,
                    only=args.only, force=args.force)


if __name__ == "__main__":
    sys.exit(main())

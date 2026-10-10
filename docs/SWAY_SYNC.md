# Sway deck exports

Microsoft Sway lecture decks are web-only and sit behind the university's Microsoft 365
login, so the Moodle API (and therefore `course_sync.py`) can never download them.
`src/sway_sync.py` exports them to Markdown instead: the deck text **and** the embedded
images, by driving a headless Chromium through Playwright with a session you sign in to once.

It finds decks by reading the `_links.md` files that course-sync writes into each section
folder, and writes each deck to `<UNIT>/Week<N>/<deck title>.md`, beside the lesson PDFs.
The week comes from the deck title's `W<week>L<lesson>` prefix (a title without one is
written to the unit folder itself).

## Setup

sway_sync runs in its own virtualenv, separate from course_sync's, because it needs
Playwright and a Chromium download and none of course_sync's dependencies. From the repo root:

```bash
python3 -m venv .venv-sway
.venv-sway/bin/pip install -r requirements/sway_sync.txt
.venv-sway/bin/playwright install chromium
```

Because `.venv-sway` has no PyYAML, `src/sway_sync.py` cannot import `src/course_sync.py` and reads
`config.yaml` with a small reader that only understands the shapes the config uses.

## Which units, and where output goes

Both come from the same `config.yaml` that course-sync uses:

- **Units to scan** = `courses[].folder` (or `code` when `folder` is omitted), in config order.
- **Output root** = `output_dir`, resolved against the config file's folder, so `output_dir: ..`
  means the folder above this repo.

Adding a unit to `config.yaml` is therefore enough; there is no list to edit in the script. If
`config.yaml` is missing, or is written in a form the reader does not understand (YAML flow style
such as `courses: [{code: A}]`, anchors, multi-line values), sway_sync prints a note on stderr and
falls back to the built-in layout: the folder above this repo and the units in
`FALLBACK_UNIT_DIRS` at the top of `src/sway_sync.py`.

## Running

Run from the repo root:

```bash
# ONE-TIME: a window opens; sign in to your university's Microsoft account and pick
# "Stay signed in: Yes". The session is saved and the window closes itself.
.venv-sway/bin/python src/sway_sync.py --login

.venv-sway/bin/python src/sway_sync.py                 # export all decks (headless, schedulable)
.venv-sway/bin/python src/sway_sync.py --check         # report new/changed decks, write nothing
.venv-sway/bin/python src/sway_sync.py --only W1L4     # just the decks whose title matches
.venv-sway/bin/python src/sway_sync.py --no-images     # text-only export
.venv-sway/bin/python src/sway_sync.py --force         # re-export unchanged decks too; use
                                                       # after changing how markdown is built
```

- Only rewrites a file when the deck content actually changed (hashes in `.sway-state.json`, repo root).
- Exit codes: 0 ok · 1 some decks failed · 2 session expired (re-run `--login`) ·
  3 `--check` found changes. A session typically lasts weeks; every successful run
  refreshes it. When it dies, the tool says so clearly instead of writing garbage.
- Session lives in `.sway-auth.json` in the repo root (treat it like a password; it is
  chmod 600 and gitignored), with the browser profile in `.sway-profile/`. Both are local-only.
- `scripts/sync_all.sh` (or the root `sync_all.sh` shim) runs course-sync first and sway_sync
  second. The order matters: sway_sync finds its deck URLs in the `_links.md` files that
  course-sync has just written.

## What is exported

- Deck text, with Sway's UI chrome stripped. Code examples survive as real text.
- Deck-hosted images, downloaded into `<deck name>_images/` beside the markdown and
  referenced inline, e.g. `UNIT1000/Week1/W1L4 - Example deck_images/01-AbC123xyz.jpg`.
  Only images hosted by the deck itself (`sway.cloud.microsoft/s/<deck>/images/<id>`)
  are taken; anything on `eus-cdn.sway.static.microsoft` is Sway's own UI furniture
  (spinners, layout-picker icons, the theme's decorative `story.png`) and is skipped.

### How images are placed

**Placement is inferred, not exact.** Each image is written directly below whatever deck
text sat immediately above it in the DOM ("the anchor"). Anything unmatchable goes to a
"Deck images that could not be placed" section at the end rather than being dropped or
guessed at; an anchor that is too short or ambiguous to place safely (a slide number such
as `01`, say) is listed there too. Three rules, each added because a real deck broke the
previous one:

- Matching moves **forward** from the last placed image, so a heading that repeats in a
  deck ("In-class Activity") resolves to two different lines rather than stacking both
  images on the first.
- If the anchor is not found per-line, it is retried against a whitespace-flattened copy
  of the whole deck. Sway's text wraps mid-sentence, so a long instruction can straddle
  two lines and be invisible to a per-line search.
- If it is still not found, it is retried from the **previous** image's line. A deck can
  put two images under one instruction, in which case the phrase occurs once and the first
  image has already moved the cursor past it; the second then stacks beneath the same
  anchor.

Sway's `alt` attribute holds its own emphasis setting (`(Moderate)`, `(Less important)`),
never a caption, so the markdown alt text is the anchor text.

### How images have to be fetched

These are the parts most likely to break later:

- Sway **virtualises**. An `<img>` only has a `src` while it is near the current scroll
  offset, so a single snapshot at the end of the pass sees a fraction of the deck. The tool
  harvests at every scroll step and accumulates by Sway's image id, scrolling in 600px
  steps against a 1000px viewport so no band goes past unseen.
- **Scroll `#storyroot`, not the document** (see the failure mode below). The loop ends on
  scroll position reaching the bottom, never on text length going quiet, and sits at the
  bottom for four more passes so the last images finish loading.
- **Deck order is not sighting order.** The table-of-contents strip preloads some section
  thumbnails before the pass reaches the body, so images are re-sorted by where their
  anchor first appears in the text before they are numbered or placed. Without this, images
  sighted late are numbered after the placement cursor has already walked past their
  anchors, and fall into the unplaced section.
- The change hash covers the image **ids** as well as the text, so a deck that swaps a
  diagram without touching a word is still detected. Ids are sufficient because Sway mints
  a new one per upload; hashing the bytes would mean downloading every image on every
  `--check`.
- Nothing is deleted except files this tool wrote itself (names of the form
  `NN-<imageId>.<ext>` inside the deck's own `_images/` folder), and only after every image
  of the deck downloaded. If a deck is re-cut and its images renumber, stale files of that
  pattern are removed on the next fully successful export; anything you add by hand to that
  folder is left alone.

## A failure mode worth knowing: a scroll that silently did nothing

An early version of the image support assigned to `document.scrollingElement.scrollTop`.
Sway scrolls an inner `#storyroot` div and the document itself does not scroll, so **the
scroll was a no-op on every deck** and only the initially-rendered images were ever seen.

Nothing looked wrong, because Sway renders all of its **text** on load and lazy-loads only
images. The text export was complete, and the deck-finished check was based on text length
going quiet, so the tool declared success without ever scrolling. A later scan that was
meant to confirm which images a deck contained ran through the same broken scroll, so it
could only ever confirm the bug, and notes written from it wrongly claimed some images did
not exist in the deck.

The lesson is general: two agreeing runs of the same broken method are not corroboration.
Check a deck's image count by opening it in the browser and looking, not only by re-running
the tool.

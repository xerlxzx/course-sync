#!/bin/zsh
# Full sync: Moodle files via course-sync, then Sway decks via sway_sync.
# Order matters - sway_sync discovers deck URLs from the _links.md files
# that course-sync writes.
# Runs from the repo root so config.yaml, the token and both venvs resolve
# the same way however this script was started.
#
# usage: sync_all.sh [--dry-run]
#   --dry-run  preview only: course_sync.py --dry-run, then sway_sync.py --check
#              (nothing is written; sway_sync's exit 3, "changes found", is
#              reported but is not a failure)
set -uo pipefail

usage() {
  echo "usage: sync_all.sh [--dry-run]"
  echo "  (no argument)  sync Moodle files, then Sway decks"
  echo "  --dry-run      preview both without writing anything"
}

dry_run=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "sync_all.sh: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

cs_args=()
sw_args=()
cs_title="Moodle files"
if (( dry_run )); then
  cs_args=(--dry-run)
  sw_args=(--check)
  cs_title="Moodle files, dry run"
fi

echo "=== 1/2 course-sync ($cs_title) ==="
.venv/bin/python src/course_sync.py "${cs_args[@]}"
cs=$?

echo ""
echo "=== 2/2 sway_sync (Sway lecture decks) ==="
.venv-sway/bin/python src/sway_sync.py "${sw_args[@]}"
sw=$?

# --check exits 3 when it finds changes; in a preview that is information.
sw_fail=$sw
if (( dry_run && sw == 3 )); then sw_fail=0; fi

echo ""
if [ $cs -ne 0 ]; then echo "WARNING: course-sync exited $cs"; fi
if [ $sw -eq 2 ]; then
  echo "WARNING: Sway session expired - run: .venv-sway/bin/python src/sway_sync.py --login"
elif (( dry_run && sw == 3 )); then
  echo "NOTE: sway_sync found changes (preview only, nothing was written)"
elif [ $sw -ne 0 ]; then
  echo "WARNING: sway_sync exited $sw"
fi
exit $(( cs != 0 || sw_fail != 0 ))

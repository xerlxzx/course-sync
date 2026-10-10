#!/bin/zsh
# Compatibility shim. The real script is scripts/sync_all.sh; this stays at the
# repo root so launchers that do `cd <repo> && exec ./sync_all.sh` keep working.
exec "$(dirname "$0")/scripts/sync_all.sh" "$@"

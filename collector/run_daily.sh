#!/usr/bin/env bash
# Daily stage-1 collection: refresh both directories and update the instance.
#
# Small updates (a few hundred writes) go through the HTTP API. The hourly
# request cap on PocketHost is the reason: a full re-write would need thousands
# of calls. Pass --full for the offline SQLite path, which is the only correct
# way to load a large volume (see collector/sqlstage.py and the README).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1090
source "$HOME/dotfiles/claude/hooks/agent-env.sh"

mode="${1:-api}"
case "$mode" in
  --full|full)
    echo "full load: power off -> sqlite stage -> upload -> power on" >&2
    echo "run by hand; a scheduled job must not power-cycle the instance" >&2
    exit 64
    ;;
esac

exec python3 -m collector.sync

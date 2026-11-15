#!/usr/bin/env bash
# Capture the three daily sources online, then perform a journaled offline load.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1090
source "$HOME/dotfiles/claude/hooks/agent-env.sh"
exec python3 -m collector.offline --sources awesome,selfhst,gh_trending "$@"

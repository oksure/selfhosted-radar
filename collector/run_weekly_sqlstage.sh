#!/usr/bin/env bash
# Compatibility entry point: full weekly collection via the journaled batch API.
# Uses the daily writer's lock and never powers down or replaces the live database.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1090
source "$HOME/dotfiles/claude/hooks/agent-env.sh"

exec python3 -m collector.sync --sources awesome,selfhst,gh_trending,gh_popular \
  --max-writes 60000 --batch-size 50 --max-runtime-seconds 14400

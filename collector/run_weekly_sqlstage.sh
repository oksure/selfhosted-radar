#!/usr/bin/env bash
# Retain the existing weekly entry point and its four-source scope.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1090
source "$HOME/dotfiles/claude/hooks/agent-env.sh"
exec python3 -m collector.offline --sources awesome,selfhst,gh_trending,gh_popular "$@"

#!/usr/bin/env bash
# Retain the existing weekly entry point and its four-source scope.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1090
source "$HOME/dotfiles/claude/hooks/agent-env.sh"

# phost (read-only power status) and the v5 pockethost CLI (code) live in
# ~/.local/bin. agent-env.sh supplies platform auth and the shared pbt_ key;
# pb_data backups use the per-instance automation superuser via pb-credentials.
export PATH="$HOME/.local/bin:$HOME/.local/node/bin:$PATH"
for tool in phost python3; do
  command -v "$tool" >/dev/null || { echo "missing $tool in PATH" >&2; exit 1; }
done
: "${PH_EMAIL:?PH_EMAIL is required for phost status}"

exec python3 -m collector.offline --sources awesome,selfhst,gh_trending,gh_popular "$@"

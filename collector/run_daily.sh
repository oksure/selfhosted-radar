#!/usr/bin/env bash
# Capture the three daily sources online, then perform a journaled offline load.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1090
source "$HOME/dotfiles/claude/hooks/agent-env.sh"

# phost (power) and the v5 pockethost CLI (code) live in ~/.local/bin; the SFTP
# client is the system OpenSSH build. agent-env.sh supplies PH_EMAIL and the
# pbt_ POCKETHOST_TOKEN.
export PATH="$HOME/.local/bin:$HOME/.local/node/bin:$PATH"
for tool in phost python3; do
  command -v "$tool" >/dev/null || { echo "missing $tool in PATH" >&2; exit 1; }
done
: "${PH_EMAIL:?PH_EMAIL is required for the PocketHost SFTP user}"

exec python3 -m collector.offline --sources awesome,selfhst,gh_trending "$@"

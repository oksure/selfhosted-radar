#!/usr/bin/env bash
# Weekly full collection via offline SQLite staging (PocketHost bulk path).
# Collects all sources while the site stays online, then briefly powers off
# to stage into pb_data/data.db (~10s downtime) and powers back on.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1090
source "$HOME/dotfiles/claude/hooks/agent-env.sh"

TMP_DB="$(mktemp /tmp/selfhosted_data.XXXXXX.db)"
TMP_RAW="$(mktemp /tmp/selfhosted_raw.XXXXXX.json)"
POWERED_OFF=0

cleanup() {
  rm -f "$TMP_DB" "$TMP_RAW"
  if [[ "$POWERED_OFF" -eq 1 ]]; then
    phost on selfhosted || true
  fi
}
trap cleanup EXIT

python3 -m collector.sqlstage --collect-only "$TMP_RAW"

phost off selfhosted --yes
POWERED_OFF=1

sftp -i "$HOME/.ssh/pockethost_ed25519" -P 2222 -o BatchMode=yes "${PH_EMAIL}@ftp.pockethost.io" <<EOF
get selfhosted/pb_data/data.db $TMP_DB
EOF

python3 -m collector.sqlstage "$TMP_DB" --from-collected "$TMP_RAW"

sftp -i "$HOME/.ssh/pockethost_ed25519" -P 2222 -o BatchMode=yes "${PH_EMAIL}@ftp.pockethost.io" <<EOF
put $TMP_DB selfhosted/pb_data/data.db
EOF

phost on selfhosted
POWERED_OFF=0

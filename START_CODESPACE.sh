#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

printf '\n============================================================\n'
printf ' ZVEZDOCHKA BOT v5.7 - VK-FIRST CODESPACES START\n'
printf '============================================================\n'

if [[ -z "${VK_GROUP_TOKEN:-}" || -z "${VK_GROUP_ID:-}" ]]; then
  cat <<'TXT'
ERROR: VK_GROUP_TOKEN and/or VK_GROUP_ID are not available.
GitHub -> Settings -> Secrets and variables -> Codespaces.
Then STOP and RESTART the Codespace.
TXT
  exit 2
fi

mkdir -p runtime
printf '[1/3] Preflight...\n'
python scripts/preflight.py
printf '[2/3] VK live check...\n'
python scripts/test_vk_connection.py
printf '[3/3] Starting VK Long Poll...\n'
printf 'Stop with Ctrl+C.\n\n'
exec python -m app.main

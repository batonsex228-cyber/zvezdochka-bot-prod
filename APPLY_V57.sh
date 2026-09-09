#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! -f app/version.py || ! -f app/main.py || ! -f data/seed_faq.json ]]; then
  echo "ERROR: run APPLY_V57.sh from the bot project root."
  exit 2
fi

VERSION="$(python -c 'from app.version import VERSION; print(VERSION)' 2>/dev/null || true)"
if [[ "$VERSION" != "5.7.0" ]]; then
  echo "ERROR: v5.7 files are not installed yet (current version: ${VERSION:-unknown})."
  echo "First unzip the v5.7 PATCH into this project root, then run bash APPLY_V57.sh."
  exit 2
fi

mkdir -p runtime
# Remove only obsolete updater files from the previous release. Runtime/user data is untouched.
rm -f APPLY_V56.sh PATCH_v5_6_README.md
find app scripts tests -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

# Never delete runtime/bot.sqlite3: it contains tickets, feedback, subscriptions,
# analytics and manager-added knowledge. The additive SQLite migration runs at startup/self-test.
echo "v5.7.0 installed. Existing runtime data was preserved."
echo "Next: python scripts/self_test.py"

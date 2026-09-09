#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUTF8=1
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/preflight.py
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Edit .env and set VK_GROUP_TOKEN + VK_GROUP_ID before starting."
  exit 2
fi
exec python -m app.main

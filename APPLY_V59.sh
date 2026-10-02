#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
python -c 'from app.version import VERSION; print("Installed version:", VERSION)'
python scripts/self_test.py

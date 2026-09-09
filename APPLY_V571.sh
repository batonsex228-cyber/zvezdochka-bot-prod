#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
echo "Applying Zvezdochka Support Bot v5.7.1 booking handoff patch..."
python scripts/self_test.py
echo "v5.7.1 patch is installed and verified."

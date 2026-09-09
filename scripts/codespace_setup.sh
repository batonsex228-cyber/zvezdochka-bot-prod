#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo ""
echo "============================================================"
echo " ZVEZDOCHKA BOT - GITHUB CODESPACES SETUP"
echo "============================================================"
echo "[1/4] Python: $(python --version 2>&1)"
echo "[2/4] Installing pinned dependencies..."
python -m pip install --disable-pip-version-check -r requirements.txt

echo "[3/4] Preparing runtime directory..."
mkdir -p runtime

echo "[4/4] Running project self-test..."
python scripts/self_test.py

echo ""
echo "SETUP COMPLETE"
echo "Next: bash START_CODESPACE.sh"
echo "============================================================"

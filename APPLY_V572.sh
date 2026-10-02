#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/3] Checking installed version..."
python - <<'PY'
from app.version import VERSION
print(f"Current files report version: {VERSION}")
if VERSION != "5.7.2":
    raise SystemExit("ERROR: v5.7.2 patch files are not installed in this directory")
PY

echo "[2/3] Running offline verification..."
python scripts/self_test.py

echo "[3/3] Patch installed."
echo "v5.7.2 VK send hotfix is installed and verified."
echo "Next: python scripts/test_vk_connection.py"
echo "Then restart bot: bash START_CODESPACE.sh"

#!/usr/bin/env bash
set -euo pipefail

echo "v6.0.3 files are already present after extracting the patch into the repository root."
python scripts/self_test.py

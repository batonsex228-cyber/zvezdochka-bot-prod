#!/usr/bin/env bash
set -euo pipefail

echo 'v6.0.4 Shift Applications: checking project (run from repository root)'
python scripts/self_test.py
node google_apps_script/self_test.js
python scripts/preflight.py

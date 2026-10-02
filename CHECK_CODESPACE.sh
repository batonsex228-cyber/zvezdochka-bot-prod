#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python scripts/self_test.py
python scripts/test_vk_connection.py

#!/usr/bin/env bash
set -u
cat <<'TXT'

============================================================
 ZVEZDOCHKA BOT v5.7 - VK-FIRST
============================================================
Required Codespaces secrets:
  VK_GROUP_TOKEN
  VK_GROUP_ID
Recommended:
  VK_MANAGER_USER_ID
Optional later:
  VK_CONTENT_TOKEN

Start: bash START_CODESPACE.sh
Test:  python scripts/self_test.py
============================================================
TXT

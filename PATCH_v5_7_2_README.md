# PATCH v5.7.2 — VK `messages.send` error 943 hotfix

Apply this patch to v5.7.1.

## Why
VK can reject `intent=customer_support` with API error 943 (`Cannot use this intent`).
This blocked ordinary bot answers and manager ticket notifications even though routing and knowledge lookup worked.

## Fix
- ordinary bot messages no longer send a VK `intent` parameter;
- if an explicitly requested optional intent is rejected with error 943, the bot retries once without it;
- newsletter `non_promo_newsletter` semantics are unchanged and are not silently bypassed.

## Codespaces
```bash
unzip -o zvezdochka_support_bot_v5_7_2_VK_943_HOTFIX_PATCH.zip -d /workspaces/zvezdochka-bot-test/zvezdochka_support_bot_v5_7_1_clean
cd /workspaces/zvezdochka-bot-test/zvezdochka_support_bot_v5_7_1_clean
chmod +x APPLY_V572.sh
./APPLY_V572.sh
python scripts/test_vk_connection.py
bash START_CODESPACE.sh
```

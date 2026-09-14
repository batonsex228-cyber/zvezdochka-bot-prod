# PATCH v5.8.0

База: `zvezdochka_support_bot_v5_7_4_GREETING_UX_HOTFIX_FULL_CLEAN.zip`.

Патч обновляет код, FAQ, ссылки, тесты и документацию. Папка `runtime/` и база `runtime/bot.sqlite3` в патч не входят, поэтому накопленные обращения, обратная связь и знания менеджера не перезаписываются.

После распаковки патча поверх v5.7.4 выполните:

```bash
python scripts/self_test.py
python scripts/test_vk_connection.py
```

Затем запустите бот обычным способом.

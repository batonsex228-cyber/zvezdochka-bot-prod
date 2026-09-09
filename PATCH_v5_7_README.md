# PATCH v5.7 — LIVE LANGUAGE + MANAGER KNOWLEDGE

Этот PATCH накладывается поверх рабочей v5.6 VK-FIRST и сохраняет `runtime/bot.sqlite3`.

## Установка в текущем Codespace

```bash
cd /путь/к/текущему/zvezdochka_support_bot
unzip -o zvezdochka_support_bot_v5_7_LIVE_LANGUAGE_PATCH.zip -d .
bash APPLY_V57.sh
python -c "from app.version import VERSION; print(VERSION)"
python scripts/self_test.py
python scripts/test_vk_connection.py
bash START_CODESPACE.sh
```

Ожидаемая версия: `5.7.0`.

`APPLY_V57.sh` не удаляет `runtime/bot.sqlite3`. Миграция таблицы базы знаний происходит автоматически и является добавочной.

После запуска менеджер может написать `#меню` → `📚 База знаний`.

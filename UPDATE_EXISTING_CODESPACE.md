# Обновление v5.6 → v5.7 в существующем Codespace

1. Остановите текущий бот (`Ctrl+C`).
2. Скопируйте PATCH-архив в корень проекта.
3. Выполните:

```bash
unzip -o zvezdochka_support_bot_v5_7_LIVE_LANGUAGE_PATCH.zip -d .
bash APPLY_V57.sh
python -c "from app.version import VERSION; print(VERSION)"
python scripts/self_test.py
python scripts/test_vk_connection.py
bash START_CODESPACE.sh
```

Ожидаемая версия: `5.7.0`.

`runtime/bot.sqlite3` не удаляется: в ней остаются тикеты, оценки, подписчики, аналитика и добавленные менеджером знания. При первом запуске v5.7 недостающие колонки базы знаний добавляются автоматически.

После запуска: `#меню` → `📚 База знаний`.

# PATCH v6.0.2 — UX POLISH

Применять поверх **v6.0.1 BOOKING TRANSPORT HOTFIX**.

Патч меняет только Python/UX-часть бота и тесты. Google Apps Script заново обновлять не нужно: `google_apps_script/Code.gs` в v6.0.2 идентичен v6.0.1.

После распаковки поверх репозитория:

```bash
python scripts/self_test.py
python -m unittest -v tests.test_v602
```

После зелёных тестов можно commit/push. На сервере обновить Docker image обычным способом. Базу SQLite и Google Sheet пересоздавать не нужно.

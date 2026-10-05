# PATCH v6.0.3 — FINAL REAUDITED · RENTALS + MANAGER SHEET + PRICING

Применять поверх **v6.0.2 UX POLISH**.

Финальный повторный аудит: **374/374** общих теста и **34/34** focused-теста v6.0.3.

После распаковки в корень репозитория:

```bash
python scripts/self_test.py
python -m unittest -v tests.test_v603
node google_apps_script/self_test.js
python scripts/preflight.py
```

В v6.0.3 меняется `google_apps_script/Code.gs`, поэтому после зелёных тестов нужно:
1. заменить Code.gs в существующем Apps Script проекте;
2. один раз запустить `setupSpreadsheet()` — он безопасно обновит схему, добавит цены и создаст `Бронирования — менеджер`;
3. обновить существующее Web App deployment новой версией, не создавая новый URL;
4. проверить `/exec` — версия должна быть `5.9.2`;
5. выполнить `python scripts/test_booking_connection.py`.

Секрет API менять не требуется. `.env` и runtime-база в патч не входят.

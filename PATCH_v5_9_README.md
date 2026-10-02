# PATCH v5.9.0

База: `zvezdochka_support_bot_v5_8_0_DOCUMENTS_KB_FULL_CLEAN.zip`.

Патч добавляет booking-модуль и first-contact UX fix. `runtime/`, `.env` и реальные токены в патч не входят.

После распаковки поверх v5.8.0:

```bash
python scripts/self_test.py
python scripts/test_vk_connection.py
```

До настройки Google Таблицы оставьте:

```env
BOOKINGS_ENABLED=false
```

Когда Google Apps Script будет развёрнут, добавьте `BOOKING_API_URL`, `BOOKING_API_SECRET` и переключите `BOOKINGS_ENABLED=true`. Перед рабочим запуском выполните безопасную проверку (она не создаёт бронь):

```bash
python scripts/test_booking_connection.py
```

Инструкция: `google_apps_script/SETUP_RU.md`.

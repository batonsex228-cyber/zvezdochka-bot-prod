# PATCH v6.0.0 — SMART HANDOFF + KNOWLEDGE LOOP

База патча: **финальная audited v5.9.0 BOOKING** (`zvezdochka_support_bot_v5_9_0_BOOKING_FINAL_AUDITED_FULL_CLEAN.zip`).

Этот архив нужно распаковывать поверх именно v5.9.0. `runtime/`, `.env`, SQLite и реальные токены в патч не входят и не заменяются.

После распаковки:

```bash
python scripts/self_test.py
python scripts/test_vk_connection.py
```

Для самого безопасного первого запуска оставьте новые функции выключенными:

```env
SMART_HANDOFF_ENABLED=false
KNOWLEDGE_LOOP_ENABLED=false
```

После того как образ запущен и обычный FAQ / handoff / Booking работают как раньше, включите:

```env
SMART_HANDOFF_ENABLED=true
SMART_HANDOFF_TTL_MINUTES=90
KNOWLEDGE_LOOP_ENABLED=true
```

и перезапустите контейнер. Новый push/rebuild для переключения флагов не нужен.

Booking v5.9 настраивается отдельно и по-прежнему управляется `BOOKINGS_ENABLED`. v6.0 не требует нового Google API, нового аккаунта или внешней CRM.


### Статус финального дополнительного аудита

Перед отправкой на сервер этот PATCH заново проверен поверх точной audited v5.9.0. Финальный offline-набор: **323/323 PASS** + Google Apps Script self-test PASS + миграция SQLite v5.9→v6.0 PASS. Дополнительно проверены переходы между Booking и Smart Handoff, освобождение Booking hold при срочном обращении, fail-closed handoff при выключенном Booking backend и защита платёжных секретов независимо от feature flag и fail-closed передача менеджеру запросов на изменение/отмену уже существующей брони. Используйте только архив с пометкой `FINAL_LAST_AUDITED`; предыдущие v6.0 release-candidate архивы считаются устаревшими.

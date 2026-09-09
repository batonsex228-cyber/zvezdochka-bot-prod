# Changelog v5.6.0 — VK-FIRST Release Candidate

## Архитектура
- Удалена обязательная Telegram/aiogram ветка из production runtime.
- `VK_GROUP_TOKEN` + `VK_GROUP_ID` — единственные обязательные messenger credentials.
- Core оставлен платформенно-независимым для будущего MAXAdapter.

## Качество ответов
- Новый rule/intent router и сущности смен.
- 15-минутный контекст коротких follow-up вопросов.
- Уточняющие кнопки вместо угадывания.
- Закреплённый ответ `shifts_prices` независим от crawler freshness.
- Источники ранжируются: approved FAQ > website > VK profile > recent VK wall.

## VK UX
- typing activity.
- Нативная карусель смен с текстовым fallback.
- feedback callbacks.
- important/unanswered support queue.
- VK manager inbox + ticket reply flow.

## Админка и аналитика
- `#меню`, `#stats`, `#open`, `#рассылка`.
- Подписка/отписка от важных уведомлений.
- Рассылки с предпросмотром, подтверждением и пакетами до 100 получателей.
- Статистика пользователей, сообщений, кнопок, автоответов, эскалаций, 👍/👎, containment, подписчиков и проблемных FAQ.
- Отрицательный feedback может создать тикет специалисту.

## Источники VK
- Профиль официального сообщества можно читать текущим community token.
- Импорт wall posts подготовлен, но `VK_CONTENT_TOKEN` опционален.
- Без content token запуск не блокируется.

## Hardening
- message_event теперь release-critical и проверяется live diagnostic.
- Newsletter intent не обходится повторной отправкой как обычное сообщение при отказе VK.
- `#меню` очищает незавершённое admin-state, чтобы случайный следующий текст не ушёл как ответ на старый тикет.
- SQLite migration только additive; runtime/bot.sqlite3 не удаляется при PATCH.

## QA
- 147 автоматических offline/integration tests.
- Python compile, JSON validation, secret scan, VK-first runtime isolation, offline demo smoke, launchers and release docs checks.

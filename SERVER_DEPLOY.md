# Server deployment — v5.7 VK-FIRST

Рекомендуемый production-вариант: Linux VPS + Docker Compose.

## Необходим доступ
- исходящий HTTPS к `api.vk.com`;
- Long Poll endpoint, который выдаёт VK;
- `zvezdaglazov.ru` для обновления сайта;
- опционально OpenAI API, если включён grounded AI.

## `.env`
Создайте из `.env.example` и укажите минимум:

```text
VK_GROUP_TOKEN=...
VK_GROUP_ID=...
VK_MANAGER_USER_ID=...
```

`VK_CONTENT_TOKEN` можно оставить пустым.

## Docker

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

Volume `zvezdochka_runtime` хранит SQLite и runtime knowledge snapshot между перезапусками.

## Перед production

```bash
python scripts/self_test.py
python scripts/test_vk_connection.py
```

Сделайте резервную копию `runtime/bot.sqlite3`. Токены храните только в environment/secrets, не в Git.

# GitHub Codespaces — v5.7 VK-FIRST

## Secrets
GitHub repository → Settings → Secrets and variables → Codespaces:

Обязательно:
- `VK_GROUP_TOKEN`
- `VK_GROUP_ID`

Рекомендуется:
- `VK_MANAGER_USER_ID`

Опционально:
- `VK_CONTENT_TOKEN`
- `OPENAI_API_KEY`

После изменения Codespaces secret полностью остановите и заново запустите Codespace.

## Проверка

```bash
python scripts/self_test.py
python scripts/test_vk_connection.py
```

Live check должен показывать:

```text
VK API: OK
Long Poll server: OK
message_new: ON
message_event: ON
VK manager inbox: OK
```

`Recent VK posts: DISABLED` допустимо: `VK_CONTENT_TOKEN` пока необязателен.

## Запуск

```bash
bash START_CODESPACE.sh
```

Codespaces используется для тестирования. Для 24/7 перенесите проект на VPS/Docker.

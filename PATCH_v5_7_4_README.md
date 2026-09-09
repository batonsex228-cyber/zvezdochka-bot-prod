# PATCH v5.7.4 — Greeting UX hotfix

Накатывается поверх v5.7.3.

## Исправляет

1. Приветствия без вопроса больше не эскалируются специалисту.
2. Стикеры без текста получают дружелюбный ответ.
3. Приветствие + вопрос сохраняет приветствие в ответе и правильно маршрутизирует сам вопрос.
4. Убрано однообразное `Конечно!` из основных кнопочных ответов.

## Применение в Codespaces

Распаковать PATCH в корень `/workspaces/zvezdochka-bot-prod`, затем выполнить:

```bash
python scripts/self_test.py
```

После успешной проверки:

```bash
git add .
git commit -m "Hotfix greeting and first-contact UX v5.7.4"
git push
```

После зелёного GitHub Actions серверу достаточно подтянуть свежий `latest`:

```text
ghcr.io/batonsex228-cyber/zvezdochka-bot-prod:latest
```

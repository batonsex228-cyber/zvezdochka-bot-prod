# ДЗЛ «Звёздочка» — GitHub Actions → GHCR → сервер

Эта сборка не требует Docker Desktop на компьютере. Docker-образ собирается на серверах GitHub Actions и публикуется в GitHub Container Registry (GHCR). На сервере нужен только Docker Engine + Docker Compose plugin.

## 1. GitHub

1. Создайте отдельный production-репозиторий (рекомендуется) или используйте существующий.
2. Поместите **содержимое этой папки** в корень репозитория. В корне должны быть `Dockerfile`, `app/`, `data/`, `.github/workflows/docker-publish.yml`.
3. Не загружайте `.env`, `runtime/`, `*.sqlite3` и токены.
4. Commit + Push в ветку `main`.
5. GitHub → **Actions** → workflow **Build and publish Docker image**. После push он запускается автоматически.
6. После зелёного workflow образ будет: `ghcr.io/<github-login>/<repo>:latest` (в нижнем регистре).

Для ручной пересборки: Actions → Build and publish Docker image → Run workflow.

## 2. Доступ к GHCR

Если package публичный, сервер сможет делать `docker pull` без логина.

Если package приватный, на сервере один раз выполните:

```bash
echo 'GITHUB_TOKEN_WITH_READ_PACKAGES' | docker login ghcr.io -u YOUR_GITHUB_LOGIN --password-stdin
```

Для чтения приватного package токен должен иметь право `read:packages`.

## 3. Первый запуск на сервере

Создайте папку, например:

```bash
mkdir -p ~/zvezdochka-bot/runtime
cd ~/zvezdochka-bot
```

Скопируйте на сервер из этой сборки только:

- `docker-compose.prod.yml`
- `deploy.env.example`
- `.env.example`
- `deploy/update_from_ghcr.sh`

Подготовьте файлы:

```bash
cp .env.example .env
cp deploy.env.example deploy.env
nano .env
nano deploy.env
```

В `deploy.env`:

```text
BOT_IMAGE=ghcr.io/<github-login>/<repo>:latest
```

В `.env` минимум:

```text
VK_GROUP_ID=...
VK_GROUP_TOKEN=...
VK_MANAGER_USER_ID=...
```

Запуск:

```bash
chmod +x deploy/update_from_ghcr.sh
./deploy/update_from_ghcr.sh
```

## 4. Проверка

```bash
docker compose --env-file deploy.env -f docker-compose.prod.yml ps
docker compose --env-file deploy.env -f docker-compose.prod.yml logs -f zvezdochka-bot
```

Нормальный лог содержит запуск VK Long Poll и `LONG POLL STARTED`.

Порт на сервере открывать не требуется: бот сам устанавливает исходящие HTTPS/Long Poll соединения с VK.

## 5. Обновление

После каждого push в `main` GitHub Actions соберёт новый `:latest`. На сервере:

```bash
cd ~/zvezdochka-bot
./deploy/update_from_ghcr.sh
```

SQLite и runtime-база лежат в `./runtime` на сервере и не пропадают при замене контейнера.

## 6. Бэкап перед обновлением

```bash
cd ~/zvezdochka-bot
mkdir -p backups
cp runtime/bot.sqlite3 "backups/bot-$(date +%Y%m%d-%H%M%S).sqlite3" 2>/dev/null || true
```

## 7. Откат на конкретную сборку

GitHub Actions также публикует тег по SHA, например `sha-abc1234`. Замените в `deploy.env` `:latest` на нужный `:sha-...` и снова запустите update script.

## Важно

- `.env` никогда не коммитить.
- `runtime/` и SQLite никогда не класть внутрь Docker image.
- На production должен работать только один экземпляр Long Poll бота с одним и тем же VK group token, иначе сообщения могут обрабатываться несколькими процессами.

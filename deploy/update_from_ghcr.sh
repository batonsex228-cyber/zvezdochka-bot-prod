#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "ERROR: .env is missing. Copy .env.example to .env and add real VK credentials."
  exit 2
fi
if [[ ! -f deploy.env ]]; then
  echo "ERROR: deploy.env is missing. Copy deploy.env.example to deploy.env and set BOT_IMAGE."
  exit 2
fi

mkdir -p runtime

docker compose --env-file deploy.env -f docker-compose.prod.yml pull
docker compose --env-file deploy.env -f docker-compose.prod.yml up -d --remove-orphans

echo
echo "Deployment complete. Current container:"
docker compose --env-file deploy.env -f docker-compose.prod.yml ps

echo
echo "Recent logs:"
docker compose --env-file deploy.env -f docker-compose.prod.yml logs --tail=80 zvezdochka-bot

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

if [ ! -d .git ]; then
  echo "ERROR: ${PROJECT_ROOT} is not a git repository."
  exit 1
fi

git pull --ff-only

APP_VERSION="$(git rev-parse --short HEAD)"
APP_BUILT_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

docker compose pull tender_ai_app
APP_VERSION="${APP_VERSION}" APP_BUILT_AT="${APP_BUILT_AT}" docker compose up -d
docker compose exec -T tender_ai_app alembic upgrade head
curl -fsS http://127.0.0.1:8000/health
echo
curl -fsS http://127.0.0.1:8000/version
echo

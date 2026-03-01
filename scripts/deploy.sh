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

export APP_VERSION="$(git rev-parse --short HEAD)"
export APP_BUILT_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

docker compose up -d --build
docker compose exec tender_ai_app alembic upgrade head
curl -fsS http://127.0.0.1:8000/version
echo

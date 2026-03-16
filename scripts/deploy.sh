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
APP_IMAGE_TAG="sha-${APP_VERSION}"
export APP_IMAGE_TAG

echo "Deploy commit: ${APP_VERSION}"
echo "Expected image tag: ${APP_IMAGE_TAG}"

# Wait for immutable image tag to appear in GHCR.
PULL_MAX_ATTEMPTS=12
PULL_DELAY_SEC=10
for attempt in $(seq 1 "${PULL_MAX_ATTEMPTS}"); do
  if docker compose pull tender_ai_app; then
    echo "Image pull succeeded on attempt ${attempt}/${PULL_MAX_ATTEMPTS}"
    break
  fi
  if [ "${attempt}" -eq "${PULL_MAX_ATTEMPTS}" ]; then
    echo "ERROR: failed to pull image tag ${APP_IMAGE_TAG} from GHCR."
    exit 1
  fi
  echo "Image not available yet. Retry in ${PULL_DELAY_SEC}s (${attempt}/${PULL_MAX_ATTEMPTS})..."
  sleep "${PULL_DELAY_SEC}"
done

APP_IMAGE="$(docker compose images tender_ai_app --format json 2>/dev/null | python3 -c "import json,sys; raw=sys.stdin.read().strip(); data=(json.loads(raw) if raw.startswith('[') else ([json.loads(line) for line in raw.splitlines() if line.strip()] if raw else [])); first=(data[0] if data else {}); print((first.get('Repository','') + ':' + first.get('Tag','')) if data else 'unknown')")"
echo "App image: ${APP_IMAGE}"

# Bring up database first; do not touch app until DB credentials are verified.
docker compose up -d tender_ai_db

DB_PREFLIGHT_MAX_ATTEMPTS=5
DB_PREFLIGHT_DELAY_SEC=3
for attempt in $(seq 1 "${DB_PREFLIGHT_MAX_ATTEMPTS}"); do
  if docker compose run --rm -T --no-deps tender_ai_app python scripts/db_preflight.py; then
    echo "DB connection OK before app startup"
    break
  fi
  if [ "${attempt}" -eq "${DB_PREFLIGHT_MAX_ATTEMPTS}" ]; then
    echo "ERROR: DB preflight failed. Check DB_* / POSTGRES_* / DATABASE_URL_* consistency in .env"
    exit 1
  fi
  echo "DB preflight retry in ${DB_PREFLIGHT_DELAY_SEC}s (${attempt}/${DB_PREFLIGHT_MAX_ATTEMPTS})..."
  sleep "${DB_PREFLIGHT_DELAY_SEC}"
done

# Start/update app only after DB credentials are confirmed.
docker compose up -d tender_ai_app

ALEMBIC_MAX_ATTEMPTS=15
ALEMBIC_DELAY_SEC=2
for attempt in $(seq 1 "${ALEMBIC_MAX_ATTEMPTS}"); do
  if docker compose exec -T tender_ai_app alembic upgrade head; then
    echo "Alembic upgrade succeeded on attempt ${attempt}/${ALEMBIC_MAX_ATTEMPTS}"
    break
  fi
  if [ "${attempt}" -eq "${ALEMBIC_MAX_ATTEMPTS}" ]; then
    echo "ERROR: alembic upgrade failed after ${ALEMBIC_MAX_ATTEMPTS} attempts."
    exit 1
  fi
  sleep "${ALEMBIC_DELAY_SEC}"
done

READINESS_MAX_ATTEMPTS=20
READINESS_SLEEP_SEC=2
HEALTH_BODY=""
VERSION_BODY=""
for attempt in $(seq 1 "${READINESS_MAX_ATTEMPTS}"); do
  HEALTH_BODY="$(curl -fsS --max-time 5 http://127.0.0.1:8000/health 2>/dev/null || true)"
  VERSION_BODY="$(curl -fsS --max-time 5 http://127.0.0.1:8000/version 2>/dev/null || true)"
  VERSION_VALUE="$(printf '%s' "${VERSION_BODY}" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("version",""))' 2>/dev/null || true)"

  if [ "${HEALTH_BODY}" = '{"ok":true}' ] && [ "${VERSION_VALUE}" = "${APP_VERSION}" ]; then
    echo "Readiness OK on attempt ${attempt}/${READINESS_MAX_ATTEMPTS}"
    break
  fi

  if [ "${attempt}" -eq "${READINESS_MAX_ATTEMPTS}" ]; then
    echo "ERROR: app readiness failed."
    echo "Last /health: ${HEALTH_BODY:-<empty>}"
    echo "Last /version: ${VERSION_BODY:-<empty>}"
    docker compose ps
    exit 1
  fi

  echo "Waiting readiness (${attempt}/${READINESS_MAX_ATTEMPTS}) sleep=${READINESS_SLEEP_SEC}s health='${HEALTH_BODY:-<empty>}' version='${VERSION_VALUE:-<empty>}'"
  sleep "${READINESS_SLEEP_SEC}"
  if [ "${READINESS_SLEEP_SEC}" -lt 10 ]; then
    READINESS_SLEEP_SEC=$((READINESS_SLEEP_SEC + 1))
  fi
done

CONTAINER_ID="$(docker compose ps -q tender_ai_app)"
CONTAINER_STARTED_AT="$(docker inspect -f '{{.State.StartedAt}}' "${CONTAINER_ID}")"

echo "Container started at: ${CONTAINER_STARTED_AT}"
echo "/health: ${HEALTH_BODY}"
echo "/version: ${VERSION_BODY}"

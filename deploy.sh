#!/usr/bin/env bash
# deploy.sh — build and deploy a versioned Docker image
#
# Usage:
#   ./deploy.sh             # auto-tag: YYYY-MM-DD-{git-hash}
#   ./deploy.sh 2026-06-04  # explicit tag
#
# What it does:
#   1. Computes IMAGE_TAG = date + git short hash (or explicit arg)
#   2. Builds docker image with version build args
#   3. Restarts ONLY tender_ai_app (DB is never touched — avoids auth drift)
#   4. Tags the new image as :latest
#
# Environment: runs on the deploy host (RU server or local with Docker access).

set -euo pipefail

# ── Tag ───────────────────────────────────────────────────────────────────────
GIT_HASH=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
BUILD_DATE=$(date -u +%Y-%m-%d)
IMAGE_TAG="${1:-${BUILD_DATE}-${GIT_HASH}}"
export IMAGE_TAG

# ── Build args ────────────────────────────────────────────────────────────────
export APP_VERSION="${GIT_HASH}"
export APP_BUILT_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
export APP_VERSION_IMAGE="${IMAGE_TAG}"
export APP_BUILT_AT_IMAGE="${APP_BUILT_AT}"

echo "==> Building tender-ai-backend:${IMAGE_TAG}"
docker compose build \
  --build-arg APP_VERSION="${APP_VERSION}" \
  --build-arg APP_BUILT_AT="${APP_BUILT_AT}" \
  --build-arg APP_VERSION_IMAGE="${APP_VERSION_IMAGE}" \
  --build-arg APP_BUILT_AT_IMAGE="${APP_BUILT_AT_IMAGE}" \
  tender_ai_app

# ── Tag as latest and local ───────────────────────────────────────────────────
docker tag "tender-ai-backend:${IMAGE_TAG}" "tender-ai-backend:latest"
echo "==> Tagged tender-ai-backend:latest"
# :local keeps docker-compose.yml default (IMAGE_TAG:-local) in sync
docker tag "tender-ai-backend:${IMAGE_TAG}" "tender-ai-backend:local"
echo "==> Tagged tender-ai-backend:local"

# ── Run pending migrations ─────────────────────────────────────────────────────
echo "==> Running alembic upgrade head"
docker compose run --rm --no-deps tender_ai_app alembic upgrade head

# ── Deploy (app only — never recreate DB to avoid auth drift) ─────────────────
echo "==> Restarting tender_ai_app only"
docker compose up -d --no-deps tender_ai_app

echo "==> Done. Running image: tender-ai-backend:${IMAGE_TAG}"
docker compose ps tender_ai_app

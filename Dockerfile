FROM python:3.12-slim

WORKDIR /app

ARG APP_VERSION=unknown
ARG APP_BUILT_AT=unknown
ARG APP_VERSION_IMAGE=unknown
ARG APP_BUILT_AT_IMAGE=unknown

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_VERSION=${APP_VERSION}
ENV APP_BUILT_AT=${APP_BUILT_AT}
ENV APP_VERSION_IMAGE=${APP_VERSION_IMAGE}
ENV APP_BUILT_AT_IMAGE=${APP_BUILT_AT_IMAGE}

COPY requirements.txt ./
RUN pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt
RUN playwright install --with-deps chromium

COPY . .

CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-8000}"]

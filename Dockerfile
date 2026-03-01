FROM python:3.12-slim

WORKDIR /app

ARG APP_VERSION=unknown
ARG APP_BUILT_AT=unknown

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_VERSION=${APP_VERSION}
ENV APP_BUILT_AT=${APP_BUILT_AT}

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-8000}"]

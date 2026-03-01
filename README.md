# Tender AI Backend (Foundation + Tenders Core)

## Stack

- Python 3.12
- FastAPI
- PostgreSQL
- SQLAlchemy 2.0 (async)
- Alembic
- JWT
- Docker + docker-compose

## Run

```bash
cd /Users/user/Documents/codex/tender_ai_backend
cp .env.example .env
docker compose up --build
```

- API docs: <http://localhost:8000/docs>
- PostgreSQL: `localhost:5433`

## Migrations

```bash
# inside app container
alembic upgrade head
```

On container start migrations are applied automatically.

## API smoke test

### 1) Register company + admin

```bash
curl -sS -X POST http://localhost:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{
    "company_name": "Acme LLC",
    "inn": "1234567890",
    "ogrn": "1027700132195",
    "legal_address": "Moscow",
    "admin_email": "admin@acme.local",
    "admin_password": "StrongPass123"
  }'
```

### 2) Login

```bash
TOKEN=$(curl -sS -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@acme.local","password":"StrongPass123"}' | jq -r '.access_token')

echo "$TOKEN"
```

### 3) Create tender

```bash
curl -sS -X POST http://localhost:8000/tenders \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "eis",
    "external_id": "EIS-10001",
    "title": "Concrete works",
    "customer_name": "City Administration",
    "region": "Moscow",
    "procurement_type": "44fz",
    "nmck": 1200000,
    "status": "new"
  }'
```

### 4) List tenders by status

```bash
curl -sS "http://localhost:8000/tenders?status=new&limit=50&offset=0" \
  -H "Authorization: Bearer $TOKEN"
```

### 5) Get company profile

```bash
curl -sS http://localhost:8000/companies/me \
  -H "Authorization: Bearer $TOKEN"
```

### 6) Get current user

```bash
curl -sS http://localhost:8000/users/me \
  -H "Authorization: Bearer $TOKEN"
```

## Tender documents

### A) Upload document

```bash
curl -X POST "http://localhost:8000/tenders/<TENDER_ID>/documents" \
  -H "Authorization: Bearer $TOKEN" \
  -F "doc_type=tz" \
  -F "file=@/path/to/file.pdf"
```

### B) List documents

```bash
curl "http://localhost:8000/tenders/<TENDER_ID>/documents" \
  -H "Authorization: Bearer $TOKEN"
```

### C) Download document

```bash
curl -L "http://localhost:8000/tender-documents/<DOC_ID>/download" \
  -H "Authorization: Bearer $TOKEN" \
  -o out.bin
```

## Tender analysis

### A) Create draft analysis

```bash
curl -X POST "http://localhost:8000/tenders/<TENDER_ID>/analysis" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"summary":"Первичный разбор","requirements":{"items":[]},"missing_docs":[],"risk_flags":[]}'
```

### B) Get analysis

```bash
curl "http://localhost:8000/tenders/<TENDER_ID>/analysis" \
  -H "Authorization: Bearer $TOKEN"
```

### C) Patch analysis

```bash
curl -X PATCH "http://localhost:8000/tenders/<TENDER_ID>/analysis" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"ready","risk_flags":[{"code":"short_deadline","title":"Короткий срок","severity":"high"}]}'
```

### D) Approve analysis

```bash
curl -X POST "http://localhost:8000/tenders/<TENDER_ID>/analysis/approve" \
  -H "Authorization: Bearer $TOKEN"
```

## Tender decisions

### A) Create decision

```bash
curl -X POST "http://localhost:8000/tenders/<TENDER_ID>/decision" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"recommendation":"unsure","expected_revenue":1000000,"cogs":700000,"logistics_cost":50000,"other_costs":25000,"risk_score":35}'
```

### B) Patch decision (margin recalculation)

```bash
curl -X PATCH "http://localhost:8000/tenders/<TENDER_ID>/decision" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"expected_revenue":1200000,"cogs":800000,"logistics_cost":70000,"other_costs":30000}'
```

### C) Recommend go/no_go

```bash
curl -X POST "http://localhost:8000/tenders/<TENDER_ID>/decision/recommend" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"recommendation":"go","notes":"Маржа и риски в допуске"}'
```

### D) Get decision

```bash
curl "http://localhost:8000/tenders/<TENDER_ID>/decision" \
  -H "Authorization: Bearer $TOKEN"
```

## Tender tasks

### A) Create task

```bash
curl -X POST "http://localhost:8000/tenders/<TENDER_ID>/tasks" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"submission_deadline","title":"Подготовить заявку","description":"Собрать пакет документов","due_at":"2026-03-01T12:00:00Z"}'
```

### B) List tasks by status

```bash
curl "http://localhost:8000/tenders/<TENDER_ID>/tasks?status=pending&order_by=due_at%20asc" \
  -H "Authorization: Bearer $TOKEN"
```

### C) Mark task as done

```bash
curl -X PATCH "http://localhost:8000/tender-tasks/<TASK_ID>" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"done"}'
```

### D) Overdue processing logs

The background scheduler checks pending tasks every `TASK_SLA_CHECK_INTERVAL_MINUTES` (default 5).
If `due_at <= now`, task status becomes `overdue` and app logs:
`Task <task_id> for tender <tender_id> is overdue.`

## EIS ingestion (public, v1)

### Get ingestion settings

```bash
curl "http://localhost:8000/companies/me/ingestion-settings" \
  -H "Authorization: Bearer $TOKEN"
```

### Update ingestion settings

```bash
curl -X PATCH "http://localhost:8000/companies/me/ingestion-settings" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "eis_public": {
      "enabled": true,
      "interval_minutes": 30,
      "query": "гранит OR памятник OR плита",
      "law": ["44fz", "223fz"],
      "regions": ["Санкт-Петербург", "Ленинградская область"],
      "only_active": true,
      "max_pages": 2,
      "page_size": 50,
      "timeout_sec": 20,
      "rate_limit_rps": 0.5
    }
  }'
```

## RU deploy smoke

1. Deploy and start in RU environment:
`docker compose up --build -d`
2. Enable ingestion via `PATCH /companies/me/ingestion-settings`.
3. Watch ingestion logs:
`docker compose logs -f tender_ai_app | grep ingestion`
4. Expected log pattern:
`EIS ingestion done: inserted=X updated=Y`

## EIS public diagnostics (434)

Run diagnostics from host or server:

```bash
cd /opt/tender_ai_backend
bash scripts/diag_eis_public.sh
```

Script prints for both modes:
- HTTP code
- guessed content type
- first 60 lines of body

If both runs consistently return `434`/stub/captcha page, keep `eis_public.enabled=false`.

## EIS OpenData ingestion (primary)

### Find datasets

```bash
curl "http://localhost:8000/ingestion/eis-opendata/datasets?q=закуп" \
  -H "Authorization: Bearer $TOKEN"
```

### Update ingestion settings

```bash
curl -X PATCH "http://localhost:8000/companies/me/ingestion-settings" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "eis_public": {"enabled": false},
    "eis_opendata": {
      "enabled": true,
      "interval_minutes": 60,
      "dataset_ids": ["<DATASET_ID_1>", "<DATASET_ID_2>"],
      "keywords": ["гранит", "памятник", "плита", "надгроб"],
      "regions": ["Санкт-Петербург", "Ленинградская область", "Псков"],
      "laws": ["44fz", "223fz"],
      "max_files_per_run": 2,
      "max_records_per_file": 20000,
      "download_timeout_sec": 60,
      "rate_limit_rps": 0.2,
      "storage_dir": "/data/opendata_cache"
    }
  }'
```

### Manual run once

```bash
curl -X POST "http://localhost:8000/ingestion/eis-opendata/run-once" \
  -H "Authorization: Bearer $TOKEN"
```

Expected log pattern:
`EIS_OPENDATA ingestion done: company_id=... datasets=N files=M inserted=X updated=Y skipped=Z duration_ms=...`

## EIS OpenData discovery (RU)

```bash
cd /opt/tender_ai_backend
bash scripts/diag_eis_opendata_discovery.sh /root/opendata_discovery.txt
```

Script saves:
- OpenData HTML status/body sample
- script links from page
- API-like strings from HTML/JS
- probe of typical opendata URLs

## EIS public maintenance cooldown

If `eis_public` receives HTTP `434` or maintenance markers in body, ingestion sets:
`ingestion_settings.eis_public.state.cooldown_until = now + 6h`

Scheduler skips `eis_public` runs for the company until cooldown expires.

## Ingestion recovery health

```bash
curl "http://localhost:8000/ingestion/health" -H "Authorization: Bearer $TOKEN"
```

Shows:
- `eis_public` cooldown
- `eis_opendata.discovery` status/cooldown/last_success/endpoints
- scheduler last run stats

To allow automatic demo import when `dataset_ids` is empty:

```json
{
  "eis_opendata": {
    "allow_demo": true
  }
}
```

## Tender alerts digest

### Summary counts

```bash
curl "http://localhost:8000/alerts/summary" \
  -H "Authorization: Bearer $TOKEN"
```

### Digest items

```bash
curl "http://localhost:8000/alerts/tenders?include_acknowledged=false" \
  -H "Authorization: Bearer $TOKEN"
```

### Acknowledge alert item

```bash
curl -X POST "http://localhost:8000/alerts/tenders/<TENDER_ID>/ack" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"category":"deadline_soon"}'
```

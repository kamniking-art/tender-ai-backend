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

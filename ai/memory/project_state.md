# Project State — Tender AI Backend

Last updated: 2026-06-05

## Business Profile v1 — status

### Implemented
- `companies.profile` JSONB gains 4 new keys: `service_regions`, `min_nmck`, `max_nmck`, `max_active_projects`
- No DB migration needed — stored in existing JSONB column
- `FitScoreComponents` has `region_ok` and `nmck_range_ok` fields (schema.py)
- `FitScorer._region()` — applies -15 penalty when tender region not in `service_regions`
- `FitScorer._nmck_range()` — applies -15 penalty when tender NMCK outside [min_nmck, max_nmck]
- Both penalties are **additive and downward only** — never inflate fit_score
- Region injected via `extracted._tender_region` attribute (injected by callers before FitScorer.score())
- `_CompanyFullProfileResponse` / `_CompanyFullProfilePatch` include all 4 fields
- UI: section «9. Операционный профиль» in `/web/company/profile`
- Tests: 20/20 passing in `tests/test_fit_scorer_business_profile.py`

### Storage-only (no effect on scoring yet)
- `max_active_projects` — saved to profile, visible in UI, but **NOT used by FitScorer or DecisionEngine**
  - Deferred: capacity constraint logic (block tenders when active project count ≥ max) not implemented
  - Tracking issue: add `active_projects_count` to facts, add policy check in decision engine

### Not implemented
- `BusinessProfileV1` Pydantic model — **does not exist** (`app/company/profile_schema.py` does not exist)
  - Profile fields are stored as raw JSONB keys, validated only at read-time via `_CompanyFullProfilePatch`
  - Deferred: add `BusinessProfileV1` as a standalone validator if stricter validation is needed

### Live verification (2026-06-05)
- DB: `service_regions`, `min_nmck`, `max_nmck`, `max_active_projects` saved and read correctly via SQLAlchemy
- API GET `/web/api/company/profile` returns all 4 keys (verified via asyncio script in container)
- API PATCH persists via `{**current_profile, **data}` merge in `web_patch_company_profile`
- Cookie auth token: web login required (API key login not applicable for web routes)

## Decision Engine — accuracy history

| Date | Accuracy | FALSE GO | FALSE NO_GO | Notes |
|------|----------|----------|-------------|-------|
| 2026-06-05 baseline | 58.9% | 21 | 7 | Before fixes |
| 2026-06-05 keyword fix | 60.3% | 13 | 7 | keyword_points 40→10 |
| 2026-06-05 okved cap | 60.3% | 3 | 7 | okved_match=False → cap at review |

**Remaining 3 FALSE GO**: capacity (1), geography (1), other (1) — not agent bugs, situational human decisions

**7 FALSE NO_GO**: all caused by `policy_block: дедлайн < 24ч` — policy too aggressive, consider softening

## Eval Dataset
- 73 labeled tenders (company: Tender AI LLC, id: 398e5b0e-dd70-40d0-a967-8884b73ad722)
- `reason` field added: `profile_mismatch | geography | deadline | capacity | requirements | other`
- Script: `python scripts/run_eval_dataset.py [--company-id UUID] [--verbose]`
- Exit 0 if accuracy ≥ 70%, exit 1 otherwise

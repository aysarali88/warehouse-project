# AGENTS.md

## Purpose
This repo is a single FastAPI app that serves two browser UIs:
- `static/materials_inventory.html`: warehouse / materials inventory system (`/` and `/warehouse`)
- `static/ftth_rollout.html`: FTTH rollout record entry and management dashboard (`/rollout`)

Main business workflows:
- Daily rollout record capture and dashboard reporting
- Warehouse stock receiving, balances, movements, technician issue
- Material Requisition workflow: create, sign, approve, issue
- Material Transfer workflow: request, approve, confirm
- Material Return workflow back to warehouse
- Scan workflow for QR / SKU / serial lookup and MR-linked scanning
- Rollout Daily Progress ingestion from Google Sheets CSV and comparison against warehouse/MR usage

## Tech Stack And Architecture
- Backend: FastAPI app in `main.py`
- ORM / DB access: SQLAlchemy in `models.py` and `database.py`
- DBs supported by code:
  - SQLite fallback (`rollout.db`) for local use
  - PostgreSQL / Supabase via `DATABASE_URL`
- Frontend: server-served static HTML with inline CSS/JS, no frontend framework
- External data source: Google Sheets CSV for Rollout Daily Progress

This is a monolith. Most backend routes, data normalization, role logic, cache logic, and business rules live in `main.py`.

## Important Files
- [main.py](</C:/Users/LENOVO/OneDrive - lnet.ly/Desktop/test/ftth-rollout-python/main.py>): app entrypoint, routes, Pydantic models, cache, auth, business logic
- [models.py](</C:/Users/LENOVO/OneDrive - lnet.ly/Desktop/test/ftth-rollout-python/models.py>): SQLAlchemy schema
- [database.py](</C:/Users/LENOVO/OneDrive - lnet.ly/Desktop/test/ftth-rollout-python/database.py>): engine/session setup and DB pool config
- [static/materials_inventory.html](</C:/Users/LENOVO/OneDrive - lnet.ly/Desktop/test/ftth-rollout-python/static/materials_inventory.html>): warehouse UI, all warehouse-side client logic
- [static/ftth_rollout.html](</C:/Users/LENOVO/OneDrive - lnet.ly/Desktop/test/ftth-rollout-python/static/ftth_rollout.html>): rollout UI
- [apps-script-rollout-dashboard/Code.gs](</C:/Users/LENOVO/OneDrive - lnet.ly/Desktop/test/ftth-rollout-python/apps-script-rollout-dashboard/Code.gs>): Google Apps Script related to rollout dashboard integration
- [rollout_daily_progress_records.json](</C:/Users/LENOVO/OneDrive - lnet.ly/Desktop/test/ftth-rollout-python/rollout_daily_progress_records.json>): local JSON data snapshot used by project workflows

## Frontend / Backend Structure
- Backend pages:
  - `/` and `/warehouse` -> `materials_inventory.html`
  - `/rollout` -> `ftth_rollout.html`
- Static files are mounted under `/static`
- `materials_inventory.html` contains:
  - login/session UI
  - nav and role-aware visibility rules
  - MR / transfer / return forms and print previews
  - dashboard rendering
  - scan modal and scan history
  - rollout progress report UI
- `ftth_rollout.html` is separate and talks mainly to `/api/records`

## Database And External Services
- Tables in `models.py` cover:
  - rollout records
  - app users
  - warehouses, technicians, products, product serials
  - stock balances, technician balances, stock movements
  - receive orders, issue orders
  - material requisitions, transfers, returns
  - scan logs and audit logs
- Google Sheets CSV:
  - configured through `ROLLOUT_DAILY_PROGRESS_CSV_URL`
  - backend fetch logic is in `main.py` (`read_rollout_daily_progress_url`, `fetch_rollout_daily_progress_csv`, `rollout_daily_progress_records`)

## Authentication And Roles
- Login API: `POST /api/auth/login`
- Roles actually used in code/UI:
  - `Admin`
  - `Management`
  - `Requester`
  - `Approval`
  - `Warehouse Manager`
- Seed/fallback users are hardcoded in `APP_USERS` in `main.py`
- Additional users can exist in `app_users` table
- UI permissions are enforced client-side in `applyPermissions()` and business gates are also enforced in backend route logic

## Main Features And Business Logic
- Product tracking supports both `bulk` and serialized items
- Serial validation matters during receive / issue / scan paths
- Scan matching uses serial number, QR code, SKU, name, or item detail
- MR issue deducts from warehouse stock and records movements
- Transfers move stock warehouse-to-warehouse only after approval + confirm
- Returns add stock back to warehouse
- Rollout usage compares rollout daily progress consumption against MR-issued quantities
- Warehouse bootstrap endpoint returns nearly all UI data and is cached for performance

## Data Flow
- Browser -> FastAPI JSON APIs -> SQLAlchemy -> DB
- `materials_inventory.html` relies heavily on `GET /api/warehouse/bootstrap`
- Rollout Daily Progress flow:
  - backend fetches Google CSV
  - normalizes rows
  - stores/serves rollout records
  - warehouse UI uses those records for progress and usage comparison

## Environment And Config
Defined or used in repo:
- `DATABASE_URL`
- `ROLLOUT_DAILY_PROGRESS_CSV_URL`
- `ROLLOUT_DAILY_PROGRESS_LIVE_CSV_URL`
- `DB_POOL_SIZE`
- `DB_MAX_OVERFLOW`
- `DB_POOL_TIMEOUT`

Do not put secrets into docs or commits. Use local `.env`.

## Commands
Local setup:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload
```

There is no checked-in test suite, build pipeline, or deployment manifest in this repo.

## Performance-Sensitive Areas
- `GET /api/warehouse/bootstrap` is the main warehouse page payload and uses cache (`WAREHOUSE_CACHE`)
- Rollout CSV fetch and parsing are cached separately
- DB pool settings in `database.py` matter for Supabase / PostgreSQL stability
- Be careful with routes that load many relations (`material requisitions`, `scan logs`, `rollout usage`)

## Constraints
- Do not change DB schema, route names, UI workflow, or role behavior unless the task explicitly requires it
- Do not refactor `main.py` broadly unless asked; it is large but central
- Do not remove fallback users or alter authentication semantics without explicit instruction
- Do not touch the `*-Copy*` files unless the task explicitly targets them; treat them as stray duplicates, not source of truth
- Preserve printed document layouts for MR / transfer / return unless the task is specifically about those print views

## Conventions Already In Use
- Single-file backend business logic in `main.py`
- Inline HTML/JS frontend with direct `fetch()` calls
- Data returned as simple dict payloads with `success: True`
- String dates are widely used instead of normalized datetime objects in business documents
- Minimal abstraction; prefer following existing patterns over introducing new layers

## Working Guidance For Future Sessions
- Inspect only files relevant to the task first:
  - auth/users -> `main.py`, `models.py`, `static/materials_inventory.html`
  - scan issues -> `main.py` scan routes/helpers, `models.py` serial/log tables, scan UI in `materials_inventory.html`
  - rollout data -> `main.py` rollout CSV helpers, `rollout_daily_progress_records.json`, `static/ftth_rollout.html`, rollout section of `materials_inventory.html`
  - stock problems -> stock routes in `main.py`, stock-related models in `models.py`
  - print/PDF views -> `materials_inventory.html`
- Expand scope only if the first-pass files prove insufficient
- Avoid unrelated cleanup/refactoring while fixing task-specific issues
- Run focused verification first:
  - API/task-specific checks before full manual smoke testing
  - broader checks only when shared logic was changed

## Feature Map
- App entry/UI routes: `main.py` near `/`, `/rollout`, `/warehouse`
- Auth/users: `main.py` auth routes + users section
- Rollout records API: `/api/records` in `main.py`
- Rollout CSV ingestion: `main.py` rollout CSV helper functions
- Warehouse summary/bootstrap: `main.py` warehouse summary/bootstrap routes
- Product + stock receive/issue/adjust: `main.py` warehouse stock routes
- MR workflow: requisition models in `models.py`, MR routes + `issue_material_requisition_row()` in `main.py`, MR UI in `materials_inventory.html`
- Transfer workflow: transfer models/routes/UI in same three files
- Return workflow: return models/routes/UI in same three files
- Scan workflow: `scan_code_candidates()`, `scan_material()`, scan-record routes in `main.py`; scan modal/history in `materials_inventory.html`

# FTTH Rollout

Small FastAPI app for tracking FTTH rollout daily progress, inventory usage, team activity, and quality checks.

## What It Does

- Opens a management dashboard at `/`.
- Saves rollout records through `/api/records`.
- Stores records in a local SQLite database.
- Shows plan vs actual progress, city/team summaries, inventory status, and worker daily entries.
- Exports records to CSV from the browser.

## Project Structure

```text
ftth-rollout-python/
  main.py                 FastAPI application and API routes
  database.py             SQLAlchemy database setup
  models.py               Rollout database model
  requirements.txt        Python dependencies
  static/ftth_rollout.html
                          Browser dashboard and worker form
```

## Run Locally

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Start the app:

```powershell
uvicorn main:app --reload
```

Then open:

```text
http://127.0.0.1:8000
```

## Supabase / PostgreSQL

Create a local `.env` file next to `main.py`:

```env
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@db.ndhccmrxpujpqhyewoac.supabase.co:5432/postgres
```

The app also works with local SQLite when `.env` is missing:

```env
DATABASE_URL=sqlite:///./rollout.db
```

## Warehouse Phase 1 APIs

- `POST /api/warehouse/warehouses` - create a warehouse.
- `POST /api/warehouse/technicians` - create a technician.
- `POST /api/warehouse/products` - create a bulk or serialized product.
- `POST /api/warehouse/receive` - receive stock into a warehouse.
- `POST /api/warehouse/issue` - issue stock from a warehouse to a technician.
- `GET /api/warehouse/stock-balances` - warehouse balances.
- `GET /api/warehouse/technician-balances` - technician balances.
- `GET /api/warehouse/summary` - warehouse counters.

## GitHub Setup

After Git is installed, run these commands inside this folder:

```powershell
git init
git add .
git commit -m "Initial FTTH rollout app"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
git push -u origin main
```

Replace `YOUR-USERNAME` and `YOUR-REPO` with your GitHub account and repository name.

## Notes

- `rollout.db` is ignored by Git because it contains local data.
- `.env` files are ignored so secrets and local settings do not get uploaded.
- Use `.env.example` to document settings that another developer may need.

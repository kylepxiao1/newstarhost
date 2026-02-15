# Analytics Notebooks

This folder contains Jupyter notebooks for querying Supabase with credentials loaded from repo env files (`app.env`, then optional `.env`).

## Setup

```powershell
.\.venv\Scripts\python.exe -m pip install -r analytics/requirements.txt
```

## Open notebooks

```powershell
.\.venv\Scripts\python.exe -m jupyter lab analytics/notebooks
```

## Notes

- Credentials are read by `analytics/notebook_utils.py`.
- `SUPABASE_URL` is optional if `SUPABASE_PROJECT_ID` is set.
- The notebooks use `SUPABASE_SECRET_KEY` first, then `SUPABASE_PUBLISHABLE_KEY`.

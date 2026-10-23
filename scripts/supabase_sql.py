"""Run a SQL file (or statement) against the Supabase database via the Management API.

    python scripts/supabase_sql.py supabase/multi_tenant.sql
    python scripts/supabase_sql.py -c "select count(*) from public.groups"

Needs SUPABASE_ACCESS_TOKEN (a personal access token) and SUPABASE_PROJECT_ID in app.env.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import _env  # noqa: E402


def run_sql(query: str) -> list:
    token = _env("SUPABASE_ACCESS_TOKEN", "").strip()
    project = _env("SUPABASE_PROJECT_ID", "").strip()
    if not token or not project:
        raise SystemExit("Missing SUPABASE_ACCESS_TOKEN or SUPABASE_PROJECT_ID in app.env")
    resp = httpx.post(
        f"https://api.supabase.com/v1/projects/{project}/database/query",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"query": query},
        timeout=120.0,
    )
    if not resp.is_success:
        raise SystemExit(f"SQL failed ({resp.status_code}): {resp.text[:2000]}")
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", nargs="?", help="SQL file to run")
    parser.add_argument("-c", "--command", help="SQL statement to run instead of a file")
    args = parser.parse_args()
    query = args.command if args.command else Path(args.file).read_text(encoding="utf-8")
    print(json.dumps(run_sql(query), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

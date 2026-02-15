from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
from supabase import Client, create_client


DEFAULT_ENV_FILES = ("app.env", ".env")


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_env(env_files: tuple[str, ...] = DEFAULT_ENV_FILES) -> dict[str, str]:
    env: dict[str, str] = {}
    root = _repo_root()
    for name in env_files:
        env.update(_read_env_file(root / name))
    for key, value in env.items():
        os.environ.setdefault(key, value)
    return env


def _resolve_supabase_url(env: dict[str, str]) -> str:
    url = (env.get("SUPABASE_URL") or os.environ.get("SUPABASE_URL") or "").strip()
    if url:
        return url.rstrip("/")
    project_id = (
        env.get("SUPABASE_PROJECT_ID") or os.environ.get("SUPABASE_PROJECT_ID") or ""
    ).strip()
    if not project_id:
        raise ValueError("Missing SUPABASE_URL or SUPABASE_PROJECT_ID.")
    return f"https://{project_id}.supabase.co"


def _resolve_supabase_key(env: dict[str, str]) -> str:
    for key_name in ("SUPABASE_SECRET_KEY", "SUPABASE_PUBLISHABLE_KEY"):
        value = (env.get(key_name) or os.environ.get(key_name) or "").strip()
        if value:
            return value
    raise ValueError("Missing SUPABASE_SECRET_KEY or SUPABASE_PUBLISHABLE_KEY.")


def get_client() -> Client:
    env = load_env()
    url = _resolve_supabase_url(env)
    key = _resolve_supabase_key(env)
    return create_client(url, key)


def fetch_rows(
    client: Client,
    table: str,
    columns: str = "*",
    limit: int = 100,
    order_by: str | None = "id",
    descending: bool = True,
) -> list[dict[str, Any]]:
    query = client.table(table).select(columns).limit(limit)
    if order_by:
        query = query.order(order_by, desc=descending)
    result = query.execute()
    return result.data or []


def table_df(
    client: Client,
    table: str,
    columns: str = "*",
    limit: int = 100,
    order_by: str | None = "id",
    descending: bool = True,
) -> pd.DataFrame:
    rows = fetch_rows(
        client=client,
        table=table,
        columns=columns,
        limit=limit,
        order_by=order_by,
        descending=descending,
    )
    return pd.DataFrame(rows)

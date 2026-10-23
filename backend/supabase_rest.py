"""Minimal synchronous Supabase client (PostgREST + Storage) using the server's secret key.

Shared by the app (group data, logins, audio uploads) and the admin/migration scripts.
"""
from __future__ import annotations

import hashlib
import threading
from typing import Any, Iterable, Optional

import httpx

from scripts.utils import _env

AUDIO_BUCKET = "song-audio"
# Audio objects are content-addressed, so they never change and can be cached for good.
AUDIO_CACHE_CONTROL = "public, max-age=31536000, immutable"


class SupabaseError(RuntimeError):
    pass


def supabase_base_url() -> str:
    url = _env("SUPABASE_URL", "").strip()
    project = _env("SUPABASE_PROJECT_ID", "").strip()
    if not url and project:
        url = f"https://{project}.supabase.co"
    return url.rstrip("/")


class SupabaseRest:
    def __init__(self, base_url: Optional[str] = None, key: Optional[str] = None) -> None:
        self.base_url = (base_url or supabase_base_url()).rstrip("/")
        self.key = key if key is not None else _env("SUPABASE_SECRET_KEY", "").strip()
        self._local = threading.local()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.key)

    def _client(self) -> httpx.Client:
        # httpx.Client isn't safe to share across threads; keep one per thread.
        client = getattr(self._local, "client", None)
        if client is None:
            client = httpx.Client(
                headers={"apikey": self.key, "Authorization": f"Bearer {self.key}"},
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
            self._local.client = client
        return client

    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if not resp.is_success:
            raise SupabaseError(f"{what} failed ({resp.status_code}): {resp.text[:500]}")
        return resp

    # --- PostgREST -------------------------------------------------------------------
    def select(self, table: str, params: Optional[dict] = None, page_size: int = 1000) -> list[dict]:
        """All rows matching params (paged)."""
        out: list[dict] = []
        offset = 0
        while True:
            q = dict(params or {})
            q.setdefault("select", "*")
            q["limit"] = page_size
            q["offset"] = offset
            resp = self._check(self._client().get(f"{self.base_url}/rest/v1/{table}", params=q), f"select {table}")
            rows = resp.json()
            out.extend(rows)
            if len(rows) < page_size:
                return out
            offset += page_size

    def select_one(self, table: str, params: dict) -> Optional[dict]:
        q = dict(params)
        q.setdefault("select", "*")
        q["limit"] = 1
        resp = self._check(self._client().get(f"{self.base_url}/rest/v1/{table}", params=q), f"select {table}")
        rows = resp.json()
        return rows[0] if rows else None

    def insert(self, table: str, rows: Any, returning: bool = False) -> list[dict]:
        headers = {"Prefer": "return=representation" if returning else "return=minimal"}
        resp = self._check(
            self._client().post(f"{self.base_url}/rest/v1/{table}", json=rows, headers=headers), f"insert {table}"
        )
        return resp.json() if returning else []

    def upsert(self, table: str, rows: Any, on_conflict: str) -> None:
        if isinstance(rows, list) and not rows:
            return
        self._check(
            self._client().post(
                f"{self.base_url}/rest/v1/{table}",
                params={"on_conflict": on_conflict},
                json=rows,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            ),
            f"upsert {table}",
        )

    def update(self, table: str, filters: dict, values: dict) -> None:
        self._check(
            self._client().patch(
                f"{self.base_url}/rest/v1/{table}", params=filters, json=values, headers={"Prefer": "return=minimal"}
            ),
            f"update {table}",
        )

    def delete(self, table: str, filters: dict) -> None:
        if not filters:
            raise SupabaseError("Refusing to delete without filters")
        self._check(
            self._client().delete(f"{self.base_url}/rest/v1/{table}", params=filters, headers={"Prefer": "return=minimal"}),
            f"delete {table}",
        )

    # --- Storage ---------------------------------------------------------------------
    def public_url(self, path: str, bucket: str = AUDIO_BUCKET) -> str:
        return f"{self.base_url}/storage/v1/object/public/{bucket}/{path}"

    def object_exists(self, path: str, bucket: str = AUDIO_BUCKET) -> bool:
        resp = self._client().head(f"{self.base_url}/storage/v1/object/public/{bucket}/{path}")
        return resp.status_code == 200

    def upload(self, path: str, data: bytes, content_type: str, bucket: str = AUDIO_BUCKET,
               cache_control: str = AUDIO_CACHE_CONTROL) -> str:
        """Upload (idempotent) and return the public CDN URL."""
        resp = self._client().post(
            f"{self.base_url}/storage/v1/object/{bucket}/{path}",
            content=data,
            headers={"Content-Type": content_type, "cache-control": cache_control, "x-upsert": "true"},
            timeout=httpx.Timeout(300.0, connect=10.0),
        )
        self._check(resp, f"upload {path}")
        return self.public_url(path, bucket)


def audio_object_path(group_id: str, data: bytes, ext: str = "mp3") -> str:
    """Content-addressed object path: same audio -> same URL, so the CDN can cache it forever."""
    return f"{group_id}/{hashlib.sha256(data).hexdigest()}.{ext.lstrip('.').lower() or 'mp3'}"


def chunked(items: Iterable, size: int) -> Iterable[list]:
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch

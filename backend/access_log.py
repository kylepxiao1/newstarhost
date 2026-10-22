"""Access logging: records who uses the app into the Supabase `access_logs` table.

Rows are queued in memory and inserted in batches by a background task, so logging never
slows down a request. Logging is a no-op when Supabase credentials are missing (e.g. the
offline bundle) and turns itself off if the table hasn't been created yet.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("access-log")

VISITOR_COOKIE = "nsh_vid"
BATCH_SIZE = 200
FLUSH_SECONDS = 5.0
QUEUE_MAX = 5000
# High-volume, low-signal requests (page assets, audio streaming, progress polling).
SKIP_PREFIXES = ("/static/", "/media/", "/dist/", "/favicon.ico", "/offline-bundle/status", "/access-log/client")
MAX_TEXT = 1000


def _clip(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value)
    return value[:MAX_TEXT]


def client_ip(headers, fallback: Optional[str]) -> Optional[str]:
    # Fly's proxy puts the real client address in Fly-Client-IP.
    ip = headers.get("fly-client-ip")
    if not ip:
        forwarded = headers.get("x-forwarded-for") or ""
        ip = forwarded.split(",")[0].strip() or None
    return ip or fallback


def request_fields(headers, fallback_ip: Optional[str]) -> dict:
    """Connection details shared by every row kind."""
    return {
        "ip": client_ip(headers, fallback_ip),
        "forwarded_for": _clip(headers.get("x-forwarded-for")),
        "fly_region": headers.get("fly-region"),
        "host": _clip(headers.get("host")),
        "user_agent": _clip(headers.get("user-agent")),
        "referer": _clip(headers.get("referer")),
        "accept_language": _clip(headers.get("accept-language")),
    }


class AccessLogger:
    def __init__(self, rest_url: str, secret_key: str) -> None:
        self._rest_url = rest_url
        self._key = secret_key
        # Created in start() so it belongs to the server's event loop.
        self._queue: "Optional[asyncio.Queue[dict]]" = None
        self._task: Optional[asyncio.Task] = None
        self.enabled = bool(rest_url and secret_key)
        if not self.enabled:
            logger.info("Access logging disabled (no Supabase credentials)")

    def log(self, row: dict) -> None:
        if not self.enabled or self._queue is None:
            return
        row.setdefault("ts", datetime.now(timezone.utc).isoformat())
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            pass

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self.enabled and self._queue is not None:
            await self._flush_all()

    async def _run(self) -> None:
        while self.enabled:
            await asyncio.sleep(FLUSH_SECONDS)
            await self._flush_all()

    async def _flush_all(self) -> None:
        while self.enabled and not self._queue.empty():
            batch = []
            while len(batch) < BATCH_SIZE and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            await self._insert(batch)

    async def _insert(self, rows: list) -> None:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(f"{self._rest_url}/access_logs", json=rows, headers=headers)
        except Exception as exc:
            logger.warning("Access log insert failed (%s rows dropped): %s", len(rows), exc)
            return
        if resp.status_code == 404 or "42P01" in resp.text or "PGRST205" in resp.text:
            logger.warning("Supabase table access_logs not found; access logging disabled until restart")
            self.enabled = False
        elif not resp.is_success:
            logger.warning("Access log insert failed status=%s body=%s", resp.status_code, resp.text[:300])

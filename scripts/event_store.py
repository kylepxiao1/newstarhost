import asyncio
import base64
import json
import logging
import math
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import betterproto
import httpx
from TikTokLive.events.proto_events import WebcastCompetitionMessage

from utils import (
    _env,
    _extract_handle,
    _extract_recipient_from_describe,
    _get_any,
    _get_attr_any,
    _is_placeholder_obj,
    _is_valid_value,
    _normalize_db_value,
    _safe_json,
    _safe_event_user
)

logger = logging.getLogger("tiktok-listener")

_ROOM_UPDATE_STATE_FIELDS = (
    "viewer_count_m_total",
    "viewer_count_total_user",
    "anonymous",
    "contributors_count",
    "rank_1_contributor_id",
    "rank_1_contributor_username",
    "rank_1_contributor_score",
    "rank_2_contributor_id",
    "rank_2_contributor_username",
    "rank_2_contributor_score",
    "rank_3_contributor_id",
    "rank_3_contributor_username",
    "rank_3_contributor_score",
    "rank_4_contributor_id",
    "rank_4_contributor_username",
    "rank_4_contributor_score",
    "rank_5_contributor_id",
    "rank_5_contributor_username",
    "rank_5_contributor_score",
)

_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def _env_int(key: str, default: int) -> int:
    value = _env(key, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _env_float(key: str, default: float) -> float:
    value = _env(key, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except Exception:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    value = _env(key, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


class SupabaseEventStore:
    def __init__(self, url: str, key: str, debug: bool = False) -> None:
        self._max_concurrent_writes = max(1, _env_int("SUPABASE_MAX_CONCURRENT_WRITES", 8))
        # Shared gate for write throughput. A semaphore prevents one slow stream
        # from serializing all writes when multiple listeners are active.
        self._lock = asyncio.Semaphore(self._max_concurrent_writes)
        self._replay_lock = asyncio.Lock()
        self._debug = debug
        self._client: Optional[httpx.AsyncClient] = None
        self._rest_url = ""
        self._raw_id_seq = int(time.time() * 1000) % 1000
        # In-memory cache keyed by (tiktok_username, room_id) to skip unchanged
        # room_update snapshots before write.
        self._room_update_last_state = {}
        self._post_retry_attempts = max(0, _env_int("SUPABASE_POST_RETRY_ATTEMPTS", 4))
        self._post_retry_base_seconds = max(0.1, _env_float("SUPABASE_POST_RETRY_BASE_SECONDS", 0.5))
        self._post_retry_max_seconds = max(
            self._post_retry_base_seconds,
            _env_float("SUPABASE_POST_RETRY_MAX_SECONDS", 10.0),
        )
        self._buffer_failed_rows = _env("SUPABASE_BUFFER_FAILED_ROWS", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        self._failed_rows_max = max(0, _env_int("SUPABASE_FAILED_ROWS_MAX", 2000))
        self._failed_rows = []
        self._failed_rows_file = _env("SUPABASE_FAILED_ROWS_FILE", "").strip()
        self._connect_timeout_restart_threshold = max(
            0, _env_int("SUPABASE_CONNECT_TIMEOUT_RESTART_THRESHOLD", 0)
        )
        self._connect_timeout_restart_window_seconds = max(
            30.0, _env_float("SUPABASE_CONNECT_TIMEOUT_RESTART_WINDOW_SECONDS", 300.0)
        )
        self._connect_timeout_restart_enabled = _env_bool(
            "SUPABASE_CONNECT_TIMEOUT_RESTART_ENABLED",
            self._connect_timeout_restart_threshold > 0,
        )
        self._connect_timeout_failures: list[float] = []
        self._restart_requested = False
        self._restart_reason = ""
        self._write_tiktok_events_raw = _env("SUPABASE_WRITE_TIKTOK_EVENTS_RAW", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        self._batch_enabled = _env_bool("SUPABASE_BATCH_ENABLED", True)
        self._batch_size = max(2, _env_int("SUPABASE_BATCH_SIZE", 10))
        self._batch_max_wait_seconds = max(
            0.05,
            _env_float("SUPABASE_BATCH_MAX_WAIT_SECONDS", 0.25),
        )
        self._batch_queue_max = max(
            self._batch_size,
            _env_int("SUPABASE_BATCH_QUEUE_MAX", 1000),
        )
        # key -> {"items": list[(row_data, future)], "task": asyncio.Task | None}
        self._batch_states = {}
        self._batch_lock = asyncio.Lock()
        self._batch_overflow_warnings = 0
        if not url or not key:
            logger.error(
                "Supabase credentials missing. Set SUPABASE_URL and SUPABASE_SECRET_KEY (or SERVICE_ROLE_KEY)."
            )
            return
        self._rest_url = url.rstrip("/") + "/rest/v1"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(connect=12.0, read=30.0, write=30.0, pool=12.0)
        keepalive_connections = max(1, _env_int("SUPABASE_HTTP_MAX_KEEPALIVE_CONNECTIONS", 10))
        max_connections = max(keepalive_connections, _env_int("SUPABASE_HTTP_MAX_CONNECTIONS", 40))
        limits = httpx.Limits(max_keepalive_connections=keepalive_connections, max_connections=max_connections)
        self._client = httpx.AsyncClient(base_url=self._rest_url, headers=headers, timeout=timeout, limits=limits)
        logger.info("Supabase REST client initialized for %s", self._rest_url)
        logger.info("Supabase write concurrency=%s", self._max_concurrent_writes)
        if self._batch_enabled:
            logger.info(
                "Supabase batching enabled (size=%s wait=%.2fs queue_max=%s)",
                self._batch_size,
                self._batch_max_wait_seconds,
                self._batch_queue_max,
            )
        else:
            logger.info("Supabase batching disabled")
        if self._write_tiktok_events_raw:
            logger.info("Supabase raw event writes enabled for tiktok_events_raw")
        else:
            logger.info(
                "Supabase raw event writes disabled for tiktok_events_raw (set SUPABASE_WRITE_TIKTOK_EVENTS_RAW=1 to enable)"
            )
        if self._buffer_failed_rows:
            self._load_failed_rows_from_disk()
            if self._failed_rows:
                logger.warning("Loaded %s buffered Supabase row writes from disk", len(self._failed_rows))
        if self._connect_timeout_restart_enabled and self._connect_timeout_restart_threshold > 0:
            logger.info(
                "Supabase ConnectTimeout restart watchdog enabled (threshold=%s, window=%ss)",
                self._connect_timeout_restart_threshold,
                int(self._connect_timeout_restart_window_seconds),
            )

    def _retry_delay(self, attempt_idx: int) -> float:
        attempt = max(0, int(attempt_idx))
        base_delay = max(0.1, float(self._post_retry_base_seconds))
        max_delay = max(base_delay, float(self._post_retry_max_seconds))
        if max_delay <= base_delay:
            delay = max_delay
        else:
            # Cap growth before exponentiation so very large retry counters
            # cannot overflow float conversion.
            max_growth_attempt = max(0, int(math.ceil(math.log2(max_delay / base_delay))))
            if attempt >= max_growth_attempt:
                delay = max_delay
            else:
                delay = min(base_delay * (2 ** attempt), max_delay)
        jitter = random.uniform(0.8, 1.2)
        return max(0.1, delay * jitter)

    def _load_failed_rows_from_disk(self) -> None:
        if not self._failed_rows_file:
            return
        path = Path(self._failed_rows_file)
        if not path.exists():
            return
        loaded = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if not isinstance(item, dict):
                    continue
                table = item.get("table")
                row_data = item.get("row_data")
                if not isinstance(table, str) or not isinstance(row_data, dict):
                    continue
                loaded.append(
                    {
                        "table": table,
                        "row_data": row_data,
                        "on_conflict": item.get("on_conflict"),
                        "prefer": item.get("prefer"),
                        "retries": int(item.get("retries") or 0),
                        "next_try_ts": float(item.get("next_try_ts") or 0),
                    }
                )
            if loaded:
                self._failed_rows.extend(loaded[-self._failed_rows_max :])
        except Exception:
            logger.exception("Failed to load buffered Supabase rows from %s", path)
        finally:
            try:
                path.write_text("", encoding="utf-8")
            except Exception:
                logger.debug("Could not clear buffered Supabase rows file %s", path)

    def _persist_failed_rows_to_disk(self) -> None:
        if not self._failed_rows_file:
            return
        path = Path(self._failed_rows_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            for item in self._failed_rows:
                lines.append(
                    _safe_json(
                        {
                            "table": item.get("table"),
                            "row_data": item.get("row_data"),
                            "on_conflict": item.get("on_conflict"),
                            "prefer": item.get("prefer"),
                            "retries": item.get("retries", 0),
                            "next_try_ts": item.get("next_try_ts", 0),
                        }
                    )
                )
            path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            logger.debug("Failed to persist buffered Supabase rows to %s", path)

    def _queue_failed_row(self, table: str, row_data: dict, on_conflict: Optional[str], prefer: Optional[str]) -> None:
        if not self._buffer_failed_rows or self._failed_rows_max <= 0:
            return
        safe_row = row_data.copy() if isinstance(row_data, dict) else {}
        entry = {
            "table": table,
            "row_data": safe_row,
            "on_conflict": on_conflict,
            "prefer": prefer,
            "retries": 0,
            "next_try_ts": 0.0,
        }
        self._failed_rows.append(entry)
        if len(self._failed_rows) > self._failed_rows_max:
            self._failed_rows = self._failed_rows[-self._failed_rows_max :]
        self._persist_failed_rows_to_disk()

    def _record_connect_timeout_failure(self, table: str) -> None:
        if (
            not self._connect_timeout_restart_enabled
            or self._connect_timeout_restart_threshold <= 0
            or self._restart_requested
        ):
            return
        now = time.time()
        window_start = now - self._connect_timeout_restart_window_seconds
        self._connect_timeout_failures = [t for t in self._connect_timeout_failures if t >= window_start]
        self._connect_timeout_failures.append(now)
        failure_count = len(self._connect_timeout_failures)
        if failure_count < self._connect_timeout_restart_threshold:
            return
        self._restart_requested = True
        self._restart_reason = (
            f"Supabase ConnectTimeout threshold reached: {failure_count} failures in "
            f"{int(self._connect_timeout_restart_window_seconds)}s (table={table})"
        )
        logger.error("%s", self._restart_reason)

    def should_force_restart(self) -> bool:
        return self._restart_requested

    def force_restart_reason(self) -> str:
        return self._restart_reason

    async def _send_post_with_retry(
        self,
        table: str,
        row_data: dict,
        on_conflict: Optional[str],
        prefer: Optional[str],
        *,
        queue_on_failure: bool,
        max_attempts: Optional[int] = None,
    ) -> Optional[httpx.Response]:
        if not self._client:
            return None
        params = {}
        prefer_header = "return=minimal"
        if on_conflict:
            params["on_conflict"] = on_conflict
            prefer_header = "resolution=merge-duplicates,return=minimal"
        if prefer:
            prefer_header = prefer

        attempts = self._post_retry_attempts if max_attempts is None else max(0, int(max_attempts))
        for attempt in range(attempts + 1):
            try:
                if self._debug:
                    logger.debug(
                        "Supabase POST %s on_conflict=%s id=%s message_id=%s attempt=%s/%s",
                        table,
                        on_conflict or "none",
                        row_data.get("id"),
                        row_data.get("message_id"),
                        attempt + 1,
                        attempts + 1,
                    )
                resp = await self._client.post(
                    f"/{table}",
                    params=params,
                    json=row_data,
                    headers={"Prefer": prefer_header},
                )
            except httpx.TransportError as exc:
                if attempt < attempts:
                    delay = self._retry_delay(attempt)
                    logger.warning(
                        "Supabase POST %s network error (%s); retrying in %.2fs (attempt %s/%s)",
                        table,
                        type(exc).__name__,
                        delay,
                        attempt + 1,
                        attempts + 1,
                    )
                    await asyncio.sleep(delay)
                    continue
                if isinstance(exc, httpx.ConnectTimeout):
                    self._record_connect_timeout_failure(table)
                if queue_on_failure:
                    self._queue_failed_row(table, row_data, on_conflict, prefer)
                logger.warning(
                    "Supabase POST %s network error (%s) after %s attempts; row buffered=%s",
                    table,
                    type(exc).__name__,
                    attempts + 1,
                    bool(queue_on_failure and self._buffer_failed_rows),
                )
                return None

            if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < attempts:
                delay = self._retry_delay(attempt)
                logger.warning(
                    "Supabase POST %s received status=%s; retrying in %.2fs (attempt %s/%s)",
                    table,
                    resp.status_code,
                    delay,
                    attempt + 1,
                    attempts + 1,
                )
                await asyncio.sleep(delay)
                continue

            if not resp.is_success and resp.status_code in _RETRYABLE_STATUS_CODES and queue_on_failure:
                self._queue_failed_row(table, row_data, on_conflict, prefer)
            return resp

        if queue_on_failure:
            self._queue_failed_row(table, row_data, on_conflict, prefer)
        return None

    async def _send_post_rows_with_retry(
        self,
        table: str,
        rows: list[dict],
        on_conflict: Optional[str],
        prefer: Optional[str],
        *,
        queue_on_failure: bool,
        max_attempts: Optional[int] = None,
    ) -> Optional[httpx.Response]:
        if not self._client:
            return None
        if not rows:
            return None
        params = {}
        prefer_header = "return=minimal"
        if on_conflict:
            params["on_conflict"] = on_conflict
            prefer_header = "resolution=merge-duplicates,return=minimal"
        if prefer:
            prefer_header = prefer

        attempts = self._post_retry_attempts if max_attempts is None else max(0, int(max_attempts))
        for attempt in range(attempts + 1):
            try:
                if self._debug:
                    logger.debug(
                        "Supabase POST batch %s on_conflict=%s size=%s attempt=%s/%s",
                        table,
                        on_conflict or "none",
                        len(rows),
                        attempt + 1,
                        attempts + 1,
                    )
                resp = await self._client.post(
                    f"/{table}",
                    params=params,
                    json=rows,
                    headers={"Prefer": prefer_header},
                )
            except httpx.TransportError as exc:
                if attempt < attempts:
                    delay = self._retry_delay(attempt)
                    logger.warning(
                        "Supabase POST batch %s network error (%s); retrying in %.2fs (attempt %s/%s)",
                        table,
                        type(exc).__name__,
                        delay,
                        attempt + 1,
                        attempts + 1,
                    )
                    await asyncio.sleep(delay)
                    continue
                if isinstance(exc, httpx.ConnectTimeout):
                    self._record_connect_timeout_failure(table)
                if queue_on_failure:
                    for row in rows:
                        self._queue_failed_row(table, row, on_conflict, prefer)
                logger.warning(
                    "Supabase POST batch %s network error (%s) after %s attempts; rows buffered=%s size=%s",
                    table,
                    type(exc).__name__,
                    attempts + 1,
                    bool(queue_on_failure and self._buffer_failed_rows),
                    len(rows),
                )
                return None

            if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < attempts:
                delay = self._retry_delay(attempt)
                logger.warning(
                    "Supabase POST batch %s received status=%s; retrying in %.2fs (attempt %s/%s)",
                    table,
                    resp.status_code,
                    delay,
                    attempt + 1,
                    attempts + 1,
                )
                await asyncio.sleep(delay)
                continue

            if not resp.is_success and resp.status_code in _RETRYABLE_STATUS_CODES and queue_on_failure:
                for row in rows:
                    self._queue_failed_row(table, row, on_conflict, prefer)
            return resp

        if queue_on_failure:
            for row in rows:
                self._queue_failed_row(table, row, on_conflict, prefer)
        return None

    def _batch_key(self, table: str, on_conflict: Optional[str], prefer: Optional[str]) -> tuple:
        return (table, on_conflict or "", prefer or "")

    def _batch_allowed(self, table: str) -> bool:
        if not self._batch_enabled:
            return False
        # Keep these writes immediate/low-latency.
        if table in {"listener_heartbeats", "tiktok_events_raw"}:
            return False
        return True

    async def _flush_batch_for_key(self, key: tuple) -> None:
        table, on_conflict_token, prefer_token = key
        on_conflict = on_conflict_token or None
        prefer = prefer_token or None
        while True:
            async with self._batch_lock:
                state = self._batch_states.get(key)
                if not state:
                    return
                queue = state.get("items") or []
                if not queue:
                    state["task"] = None
                    return
                chunk = queue[: self._batch_size]
                del queue[: len(chunk)]
                state["items"] = queue

            rows = [item[0] for item in chunk]
            futures = [item[1] for item in chunk]
            resp = await self._send_post_rows_with_retry(
                table=table,
                rows=rows,
                on_conflict=on_conflict,
                prefer=prefer,
                queue_on_failure=True,
            )

            # If a batch fails, salvage good rows by retrying per-row.
            # Special-case missing unique constraints by dropping on_conflict.
            row_on_conflict = on_conflict
            fallback_reason = None
            if on_conflict and self._response_missing_unique(resp):
                row_on_conflict = None
                fallback_reason = "missing unique constraint for on_conflict"
            elif resp is not None and not resp.is_success:
                fallback_reason = f"status={resp.status_code}"

            if fallback_reason:
                logger.warning(
                    "Supabase batch %s failed (%s); falling back to per-row writes for %s rows",
                    table,
                    fallback_reason,
                    len(rows),
                )
                for idx, row in enumerate(rows):
                    row_resp = await self._send_post_with_retry(
                        table=table,
                        row_data=row,
                        on_conflict=row_on_conflict,
                        prefer=prefer,
                        queue_on_failure=True,
                    )
                    fut = futures[idx]
                    if not fut.done():
                        fut.set_result(row_resp)
                continue

            for fut in futures:
                if not fut.done():
                    fut.set_result(resp)

    async def _flush_batch_for_key_after_delay(self, key: tuple, delay: float) -> None:
        await asyncio.sleep(max(0.01, delay))
        await self._flush_batch_for_key(key)

    async def _enqueue_batched_row(
        self,
        table: str,
        row_data: dict,
        on_conflict: Optional[str] = None,
        prefer: Optional[str] = None,
    ) -> Optional[httpx.Response]:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        key = self._batch_key(table, on_conflict, prefer)

        async with self._batch_lock:
            state = self._batch_states.get(key)
            if state is None:
                state = {"items": [], "task": None}
                self._batch_states[key] = state
            queue = state["items"]

            if len(queue) >= self._batch_queue_max:
                # Drop oldest pending item under sustained overload to avoid OOM.
                dropped_row, dropped_future = queue.pop(0)
                self._queue_failed_row(table, dropped_row, on_conflict, prefer)
                if not dropped_future.done():
                    dropped_future.set_result(None)
                self._batch_overflow_warnings += 1
                if self._batch_overflow_warnings <= 3 or self._batch_overflow_warnings % 100 == 0:
                    logger.warning(
                        "Supabase batch queue overflow for %s key=%s (max=%s, warnings=%s); "
                        "dropping oldest row to disk buffer",
                        table,
                        key,
                        self._batch_queue_max,
                        self._batch_overflow_warnings,
                    )

            queue.append((row_data.copy() if isinstance(row_data, dict) else {}, fut))
            state["items"] = queue

            current_task = state.get("task")
            should_start_now = len(queue) >= self._batch_size
            if current_task is None or current_task.done():
                if should_start_now:
                    state["task"] = asyncio.create_task(self._flush_batch_for_key(key))
                else:
                    state["task"] = asyncio.create_task(
                        self._flush_batch_for_key_after_delay(key, self._batch_max_wait_seconds)
                    )

        resp = await fut
        if resp is not None and getattr(resp, "is_success", False) and self._failed_rows:
            async with self._replay_lock:
                if self._failed_rows:
                    await self._replay_failed_rows(limit=1)
        return resp

    async def _replay_failed_rows(self, limit: int = 2) -> None:
        if not self._buffer_failed_rows or not self._failed_rows or not self._client:
            return
        processed = 0
        changed = False
        now = time.time()
        while self._failed_rows and processed < max(1, limit):
            entry = self._failed_rows[0]
            table = entry.get("table")
            row_data = entry.get("row_data")
            if not isinstance(table, str) or not isinstance(row_data, dict):
                self._failed_rows.pop(0)
                changed = True
                continue
            next_try_ts = float(entry.get("next_try_ts") or 0)
            if next_try_ts > now:
                break
            resp = await self._send_post_with_retry(
                table,
                row_data,
                entry.get("on_conflict"),
                entry.get("prefer"),
                queue_on_failure=False,
                max_attempts=1,
            )
            processed += 1
            if resp is not None and resp.is_success:
                self._failed_rows.pop(0)
                changed = True
                continue
            retries = int(entry.get("retries") or 0) + 1
            entry["retries"] = retries
            entry["next_try_ts"] = time.time() + self._retry_delay(retries)
            self._failed_rows.pop(0)
            self._failed_rows.append(entry)
            changed = True
            break
        if changed:
            self._persist_failed_rows_to_disk()

    async def _post_row(
        self,
        table: str,
        row_data: dict,
        on_conflict: Optional[str] = None,
        prefer: Optional[str] = None,
    ) -> Optional[httpx.Response]:
        if not self._client:
            return None
        if self._batch_allowed(table):
            return await self._enqueue_batched_row(
                table=table,
                row_data=row_data,
                on_conflict=on_conflict,
                prefer=prefer,
            )
        resp = await self._send_post_with_retry(
            table,
            row_data,
            on_conflict,
            prefer,
            queue_on_failure=True,
        )
        # Keep fresh/live rows first; replay backlog opportunistically afterward.
        if resp is not None and resp.is_success and self._failed_rows:
            async with self._replay_lock:
                if self._failed_rows:
                    await self._replay_failed_rows(limit=1)
        return resp

    def _log_result(self, table: str, action: str, resp: Optional[httpx.Response]) -> None:
        if resp is None:
            logger.debug("Supabase %s %s skipped (client unavailable)", table, action)
            return
        if resp.is_success:
            logger.debug("Supabase %s %s ok (status=%s)", table, action, resp.status_code)
            return
        try:
            payload = resp.json()
        except Exception:
            payload = (resp.text or "").strip()
        logger.error("Supabase %s %s failed (status=%s): %s", table, action, resp.status_code, payload)

    def _response_missing_unique(self, resp: Optional[httpx.Response]) -> bool:
        if resp is None:
            return False
        msg = ""
        code = ""
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                msg = str(payload.get("message", "")).lower()
                code = str(payload.get("code", "")).lower()
            else:
                msg = str(payload).lower()
        except Exception:
            msg = (resp.text or "").lower()
        return "no unique or exclusion constraint" in msg or "42p10" in msg or code == "42p10"

    def _next_raw_id(self) -> int:
        # millisecond timestamp * 1000 + sequence to avoid collisions within the same ms
        base = int(time.time() * 1000) * 1000
        self._raw_id_seq = (self._raw_id_seq + 1) % 1000
        return base + self._raw_id_seq

    def _next_row_id(self) -> int:
        return self._next_raw_id()

    def _to_int(self, value) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        try:
            text = str(value).strip()
        except Exception:
            return None
        if not text:
            return None
        try:
            return int(text)
        except Exception:
            try:
                return int(float(text))
            except Exception:
                return None

    def _camel_from_snake(self, value: str) -> str:
        parts = value.split("_")
        if not parts:
            return value
        return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])

    def _room_update_partition_key(self, row_data: dict) -> tuple:
        username = row_data.get("tiktok_username")
        room_id = row_data.get("room_id")
        try:
            username = str(username).strip().lower() if username is not None else ""
        except Exception:
            username = ""
        try:
            room_id = str(room_id).strip() if room_id is not None else ""
        except Exception:
            room_id = ""
        return (username, room_id)

    def _room_update_state_tuple(self, row_data: dict) -> tuple:
        return tuple(row_data.get(field) for field in _ROOM_UPDATE_STATE_FIELDS)

    async def log_competition_event(
        self,
        payload: dict,
        tiktok_username: str,
        raw_id: Optional[int] = None,
    ) -> None:
        payload_b64 = _get_any(payload, "payload")
        if not payload_b64:
            return

        raw_bytes = b""
        if isinstance(payload_b64, (bytes, bytearray)):
            raw_bytes = bytes(payload_b64)
            payload_b64 = base64.b64encode(raw_bytes).decode("ascii")
        elif isinstance(payload_b64, str):
            try:
                raw_bytes = base64.b64decode(payload_b64 + "===")
            except Exception:
                logger.warning("Failed to base64 decode competition payload")
                return
        else:
            return

        try:
            msg = WebcastCompetitionMessage().parse(raw_bytes)
            decoded = msg.to_dict()
        except Exception:
            logger.exception("Failed to decode WebcastCompetitionMessage")
            return

        active_stage = None
        for field_name in (
            "initiate",
            "reply",
            "start",
            "settle_start",
            "settle_end",
            "score_change",
            "finish",
            "switch_turn",
        ):
            try:
                if betterproto.serialized_on_wire(getattr(msg, field_name)):
                    active_stage = field_name
                    break
            except Exception:
                continue

        active_payload = None
        if active_stage:
            active_payload = decoded.get(self._camel_from_snake(active_stage))

        competition_message_type = None
        if isinstance(decoded, dict):
            raw_type = decoded.get("type")
            if isinstance(raw_type, str) and raw_type.strip():
                competition_message_type = raw_type.strip()
            elif raw_type is not None:
                enum_name = getattr(raw_type, "name", None)
                if isinstance(enum_name, str) and enum_name.strip():
                    competition_message_type = enum_name.strip()
        if not competition_message_type and active_stage:
            stage_to_type = {
                "initiate": "COMPETITION_MESSAGE_TYPE_INITIATE",
                "reply": "COMPETITION_MESSAGE_TYPE_REPLY",
                "start": "COMPETITION_MESSAGE_TYPE_START",
                "score_change": "COMPETITION_MESSAGE_TYPE_SCORE_CHANGE",
                "finish": "COMPETITION_MESSAGE_TYPE_FINISH",
                "settle_start": "COMPETITION_MESSAGE_TYPE_SETTLE_START",
                "settle_end": "COMPETITION_MESSAGE_TYPE_SETTLE_END",
                "switch_turn": "COMPETITION_MESSAGE_TYPE_SWITCH_TURN",
            }
            competition_message_type = stage_to_type.get(active_stage)

        base_message = decoded.get("baseMessage") if isinstance(decoded, dict) else {}
        if not isinstance(base_message, dict):
            base_message = {}
        biz_common = decoded.get("bizCommon") if isinstance(decoded, dict) else {}
        if not isinstance(biz_common, dict):
            biz_common = {}

        team_infos = []
        if isinstance(active_payload, dict):
            candidate = active_payload.get("teamInfos")
            if isinstance(candidate, list):
                team_infos = candidate
            if not team_infos:
                direct_teams = active_payload.get("teams")
                if isinstance(direct_teams, list):
                    team_infos = direct_teams
            if not team_infos:
                initiate_info = active_payload.get("initiateInfo")
                if isinstance(initiate_info, dict):
                    start_teams = initiate_info.get("teams")
                    if isinstance(start_teams, list):
                        team_infos = start_teams

        def _to_id_str(value) -> Optional[str]:
            if value is None:
                return None
            try:
                text = str(value).strip()
            except Exception:
                return None
            if not text:
                return None
            if text.endswith(".0"):
                text = text[:-2]
            return text

        team_scores = []
        team_member_ids = []
        for team in team_infos:
            if not isinstance(team, dict):
                continue
            score_val = self._to_int(team.get("score"))
            if score_val is not None:
                team_scores.append(score_val)
            members = team.get("members")
            if not isinstance(members, list):
                members = team.get("users")
            member_ids = []
            if isinstance(members, list):
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    user_obj = member.get("user")
                    user_id = None
                    if isinstance(user_obj, dict):
                        user_id = _to_id_str(user_obj.get("userId"))
                    if user_id is None:
                        user_id = _to_id_str(member.get("userId"))
                    if user_id is not None:
                        member_ids.append(user_id)
            team_member_ids.append(member_ids)

        end_timestamp = None
        end_timestamp_actual = None
        if isinstance(active_payload, dict):
            end_timestamp = self._to_int(
                _get_any(
                    active_payload,
                    "endTimestamp",
                    "end_timestamp",
                )
            )
            end_timestamp_actual = self._to_int(
                _get_any(
                    active_payload,
                    "actualEndTimestamp",
                    "endTimestampActual",
                    "end_timestamp_actual",
                )
            )

        now_dt = datetime.now(timezone.utc)
        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": now_dt.isoformat(),
            "unix_ts": int(now_dt.timestamp()),
            "message_id": self._to_int(base_message.get("messageId"))
            or self._to_int(_get_any(payload, "msgId", "msg_id", "messageId", "message_id")),
            "room_id": self._to_int(base_message.get("roomId")) or self._to_int(biz_common.get("roomId")),
            "create_time_ms": self._to_int(base_message.get("createTime")),
            "competition_id": self._to_int(biz_common.get("competitionId")),
            "competition_room_id": self._to_int(biz_common.get("roomId")),
            "competition_type": biz_common.get("type"),
            "competition_message_type": competition_message_type,
            "active_stage": active_stage,
            "team_count": len(team_infos) if team_infos else 0,
            "team_1_score": team_scores[0] if len(team_scores) > 0 else None,
            "team_2_score": team_scores[1] if len(team_scores) > 1 else None,
            "team_1_member_ids": team_member_ids[0] if len(team_member_ids) > 0 else [],
            "team_2_member_ids": team_member_ids[1] if len(team_member_ids) > 1 else [],
            "end_timestamp": end_timestamp,
            "end_timestamp_actual": end_timestamp_actual,
            "total_score": sum(team_scores) if team_scores else 0,
            "team_infos_json": _safe_json(team_infos),
            "tiktok_username": tiktok_username,
        }

        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping competition_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                # Keep Postgres text[] fields as JSON arrays for PostgREST (not JSON-encoded strings).
                row_data["team_1_member_ids"] = row.get("team_1_member_ids") or []
                row_data["team_2_member_ids"] = row.get("team_2_member_ids") or []
                on_conflict = "message_id,tiktok_username" if row_data.get("message_id") else None
                action = "upsert" if on_conflict else "insert"
                resp = await self._post_row("competition_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("competition_events", row_data)
                    action = "insert-fallback"
                self._log_result("competition_events", action, resp)
            except Exception:
                logger.exception("Failed to log competition event")

    async def log_gift(
        self,
        payload: dict,
        event,
        tiktok_username: str,
        gift_value_raw: Optional[int],
        gift_value_delta: Optional[int],
        raw_id: Optional[int] = None,
    ) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())
        base_message = _get_attr_any(event, "base_message", "baseMessage")
        gift_monitor_payload = None
        if isinstance(payload, dict):
            gift_monitor_payload = payload.get("gift_monitor_info") or payload.get("giftMonitorInfo")
        gift_monitor_event = _get_attr_any(event, "gift_monitor_info", "giftMonitorInfo")
        user_identity_payload = None
        if isinstance(payload, dict):
            user_identity_payload = payload.get("user_identity") or payload.get("userIdentity")
        user_identity_event = _get_attr_any(event, "user_identity", "userIdentity")
        gift = payload.get("gift") if isinstance(payload, dict) else {}
        if not isinstance(gift, dict):
            gift = {}
        gift_obj = _get_attr_any(event, "gift")
        gift_id = _get_any(gift, "id", "gift_id", "giftId") or _get_attr_any(gift_obj, "id", "gift_id", "giftId")
        gift_name = _get_any(gift, "name", "describe") or _get_attr_any(gift_obj, "name", "describe")
        diamond_count = _get_any(payload, "diamond_count", default=_get_any(gift, "diamond_count", "diamond_cost"))
        if diamond_count is None:
            diamond_count = _get_attr_any(gift_obj, "diamond_count", "diamondCost", "diamond_cost")
        repeat_count = _get_any(payload, "repeat_count", "repeatCount", default=_get_any(gift, "repeat_count"))
        if repeat_count is None:
            repeat_count = _get_attr_any(gift_obj, "repeat_count", "repeatCount")

        def _gift_monitor_val(*names):
            if gift_monitor_payload is not None:
                if isinstance(gift_monitor_payload, dict):
                    val = _get_any(gift_monitor_payload, *names)
                else:
                    val = _get_attr_any(gift_monitor_payload, *names)
                if _is_valid_value(val):
                    return val
            val = _get_attr_any(gift_monitor_event, *names)
            if _is_valid_value(val):
                return val
            return None

        def _user_identity_val(*names):
            if user_identity_payload is not None:
                if isinstance(user_identity_payload, dict):
                    val = _get_any(user_identity_payload, *names)
                else:
                    val = _get_attr_any(user_identity_payload, *names)
                if val is not None:
                    return val
            return _get_attr_any(user_identity_event, *names)

        anchor_id = _get_any(payload, "anchor_id", "anchorId") or _gift_monitor_val("anchor_id", "anchorId")
        describe_val = _get_any(payload, "describe") or _get_attr_any(base_message, "describe")
        room_heat_val = _get_any(payload, "room_message_heat_level") or _get_attr_any(
            base_message, "room_message_heat_level", "roomMessageHeatLevel"
        )
        gift_monitor_from_platform = _get_any(payload, "gift_monitor_from_platform") or _gift_monitor_val(
            "from_platform", "fromPlatform"
        )
        gift_monitor_from_version = _get_any(payload, "gift_monitor_from_version") or _gift_monitor_val(
            "from_version", "fromVersion"
        )
        gift_monitor_send_message_success_ms = _gift_monitor_val(
            "send_gift_send_message_success_ms", "sendGiftSendMessageSuccessMs"
        )
        gift_monitor_to_user_id = _gift_monitor_val("to_user_id", "toUserId")
        send_type = _get_any(payload, "send_type", "sendType")
        if not _is_valid_value(send_type):
            send_type = _get_attr_any(event, "send_type", "sendType")
        if not _is_valid_value(send_type):
            send_type = _get_any(gift, "send_type", "sendType") or _get_attr_any(gift_obj, "send_type", "sendType")
        order_id = _get_any(payload, "order_id", "orderId")
        if not _is_valid_value(order_id):
            order_id = _get_attr_any(event, "order_id", "orderId")
        fan_ticket_count = _get_any(payload, "fan_ticket_count")
        if not _is_valid_value(fan_ticket_count):
            if _is_valid_value(gift_value_raw):
                fan_ticket_count = gift_value_raw
            elif _is_valid_value(gift_value_delta):
                fan_ticket_count = gift_value_delta
            elif _is_valid_value(diamond_count):
                fan_ticket_count = diamond_count
        if not _is_valid_value(fan_ticket_count):
            fan_ticket_count = None
        room_fan_ticket_count = _get_any(payload, "room_fan_ticket_count")
        if not _is_valid_value(room_fan_ticket_count):
            room_fan_ticket_count = fan_ticket_count
        if not _is_valid_value(room_fan_ticket_count):
            room_fan_ticket_count = None
        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "event_type": "gift",
            "room_id": _get_any(payload, "room_id", "roomId") or _get_attr_any(base_message, "room_id", "roomId"),
            "create_time_ms": _get_any(payload, "create_time", "create_time_ms", "timestamp") or _get_attr_any(
                base_message, "create_time", "createTime"
            ),
            "message_id": _get_any(payload, "msg_id", "message_id", "event_id") or _get_attr_any(
                base_message, "message_id", "messageId"
            ),
            "gift_id": gift_id,
            "gift_name": gift_name,
            "diamond_count": diamond_count,
            "repeat_count": repeat_count,
            "combo_count": _get_any(payload, "combo_count", "comboCount"),
            "amount_value": gift_value_raw if gift_value_raw is not None else _get_any(payload, "amount", "total_value"),
            "fan_ticket_count": fan_ticket_count,
            "room_fan_ticket_count": room_fan_ticket_count,
            "group_count": _get_any(payload, "group_count"),
            "repeat_end": _get_any(payload, "repeat_end", "repeatEnd"),
            "from_user_id": _get_any(payload, "user_id", "userId"),
            "from_username": _get_any(payload, "user_name", "userName"),
            "from_nickname": _get_any(payload, "user_nickname", "nickname"),
            "to_user_id": _get_any(payload, "to_user_id", "toUserId"),
            "to_username": _get_any(payload, "to_user_name", "toUserName"),
            "to_nickname": _get_any(payload, "to_user_nickname", "toNickname"),
            "to_member_id_int": _get_any(payload, "to_member_id_int", "toMemberIdInt"),
            "to_member_nickname": _get_any(payload, "to_member_nickname", "toMemberNickname"),
            "anchor_id": anchor_id,
            "send_type": send_type,
            "order_id": order_id,
            "group_id": _get_any(payload, "group_id", "groupId"),
            "describe": describe_val,
            "is_gift_giver_of_anchor": _get_any(payload, "is_gift_giver_of_anchor")
            or _user_identity_val("is_gift_giver_of_anchor", "isGiftGiverOfAnchor"),
            "is_subscriber_of_anchor": _get_any(payload, "is_subscriber_of_anchor")
            or _user_identity_val("is_subscriber_of_anchor", "isSubscriberOfAnchor"),
            "is_mutual_following_with_anchor": _get_any(
                payload, "is_mutual_following_with_anchor", "is_mutual_following_of_anchor"
            )
            or _user_identity_val("is_mutual_following_with_anchor", "is_mutual_following_of_anchor"),
            "is_follower_of_anchor": _get_any(payload, "is_follower_of_anchor")
            or _user_identity_val("is_follower_of_anchor", "isFollowerOfAnchor"),
            "gift_monitor_from_platform": gift_monitor_from_platform,
            "gift_monitor_from_version": gift_monitor_from_version,
            "gift_monitor_send_msg_ms": _get_any(payload, "gift_monitor_send_msg_ms")
            or gift_monitor_send_message_success_ms,
            "priority": _get_any(payload, "priority"),
            "room_message_heat_level": room_heat_val,
            "gift_monitor_anchor_id": _gift_monitor_val("anchor_id", "anchorId"),
            "gift_monitor_to_user_id": gift_monitor_to_user_id,
            "gift_monitor_send_message_success_ms": gift_monitor_send_message_success_ms,
            "tiktok_username": tiktok_username,
        }
        if row.get("to_user_id") is not None and not _is_valid_value(row.get("to_user_id")):
            row["to_user_id"] = None
        if not row.get("from_user_id") or not row.get("from_username") or not row.get("from_nickname"):
            user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else {}
            event_user = _get_attr_any(event, "user", "user_info", "userInfo")
            if not row.get("from_user_id"):
                row["from_user_id"] = (
                    _get_any(user_payload, "id", "user_id", "userId")
                    or _get_attr_any(event_user, "id", "user_id", "userId")
                )
            if not row.get("from_username"):
                row["from_username"] = (
                    _get_any(user_payload, "unique_id", "display_id", "username")
                    or _extract_handle(event_user)
                )
            if not row.get("from_nickname"):
                row["from_nickname"] = (
                    _get_any(user_payload, "nickname", "nick_name", "nickName")
                    or _get_attr_any(event_user, "nickname", "nick_name", "nickName")
                )
        if not row.get("to_user_id") or not row.get("to_username") or not row.get("to_nickname"):
            receiver_payload = payload.get("receiver") if isinstance(payload.get("receiver"), dict) else {}
            to_user_payload = payload.get("to_user") if isinstance(payload.get("to_user"), dict) else {}
            event_to_user = _get_attr_any(event, "to_user", "receiver", "toUser")
            to_member_id = _get_any(payload, "to_member_id", "toMemberId")
            to_member_id_int = _get_any(payload, "to_member_id_int", "toMemberIdInt")
            to_member_nickname = _get_any(payload, "to_member_nickname", "toMemberNickname")
            describe = str(row.get("describe") or "")
            parsed_recipient = _extract_recipient_from_describe(describe)

            def pick_value(*values):
                for value in values:
                    if _is_valid_value(value):
                        return value
                return None

            if not row.get("to_user_id"):
                row["to_user_id"] = pick_value(
                    _get_any(payload, "to_user_id", "toUserId"),
                    _get_any(receiver_payload, "id", "user_id"),
                    _get_any(to_user_payload, "id", "user_id"),
                    _get_attr_any(event_to_user, "id", "user_id"),
                    gift_monitor_to_user_id,
                    row.get("anchor_id"),
                    to_member_id_int,
                    to_member_id,
                )
            if not row.get("to_username"):
                row["to_username"] = pick_value(
                    _get_any(payload, "to_user_name", "toUserName"),
                    _get_any(receiver_payload, "unique_id", "display_id", "username"),
                    _get_any(to_user_payload, "unique_id", "display_id", "username"),
                    _extract_handle(event_to_user),
                )
                if not row.get("to_username") and "gifted the host" in describe.lower():
                    row["to_username"] = tiktok_username
                if not row.get("to_username") and parsed_recipient:
                    candidate = parsed_recipient.lstrip("@")
                    if re.fullmatch(r"[A-Za-z0-9._]{2,}", candidate):
                        row["to_username"] = candidate
            if not row.get("to_nickname"):
                row["to_nickname"] = pick_value(
                    _get_any(payload, "to_user_nickname", "toNickname"),
                    _get_any(receiver_payload, "nickname", "nick_name", "nickName"),
                    _get_any(to_user_payload, "nickname", "nick_name", "nickName"),
                    _get_attr_any(event_to_user, "nickname", "nick_name", "nickName"),
                    to_member_nickname,
                )
                if not row.get("to_nickname") and parsed_recipient:
                    row["to_nickname"] = parsed_recipient
            if not row.get("to_user_id") and row.get("to_username") and tiktok_username:
                if row["to_username"].lstrip("@").lower() == tiktok_username.lstrip("@").lower():
                    anchor_candidate = row.get("anchor_id")
                    if _is_valid_value(anchor_candidate):
                        row["to_user_id"] = anchor_candidate
            if tiktok_username:
                host_name = tiktok_username.lstrip("@")
                if not row.get("to_username"):
                    row["to_username"] = host_name
                if not row.get("to_nickname"):
                    row["to_nickname"] = host_name
            if not row.get("to_user_id") and tiktok_username and row.get("to_username"):
                if row["to_username"].lstrip("@").lower() == tiktok_username.lstrip("@").lower():
                    anchor_candidate = row.get("anchor_id")
                    if _is_valid_value(anchor_candidate):
                        row["to_user_id"] = anchor_candidate
        to_member_id_int = row.get("to_member_id_int")
        if not _is_valid_value(to_member_id_int):
            to_member_id_int = _get_any(payload, "to_member_id", "toMemberId")
        if not _is_valid_value(to_member_id_int):
            to_member_id_int = row.get("to_user_id") or gift_monitor_to_user_id or row.get("anchor_id")
        row["to_member_id_int"] = to_member_id_int if _is_valid_value(to_member_id_int) else None
        to_member_nickname = row.get("to_member_nickname")
        if not _is_valid_value(to_member_nickname):
            to_member_nickname = row.get("to_nickname") or row.get("to_username")
        if not _is_valid_value(to_member_nickname) and describe_val:
            parsed_recipient = _extract_recipient_from_describe(str(describe_val))
            if parsed_recipient:
                to_member_nickname = parsed_recipient
        if not _is_valid_value(to_member_nickname) and describe_val:
            if "gifted the host" in str(describe_val).lower():
                to_member_nickname = tiktok_username
        row["to_member_nickname"] = to_member_nickname if _is_valid_value(to_member_nickname) else None
        if not (row.get("to_user_id") or row.get("to_username") or row.get("to_nickname")):
            debug = {}
            try:
                if isinstance(payload, dict):
                    debug["payload_to_user_id"] = _get_any(payload, "to_user_id", "toUserId")
                    debug["payload_to_user_name"] = _get_any(payload, "to_user_name", "toUserName")
                    debug["payload_to_nickname"] = _get_any(payload, "to_user_nickname", "toNickname")
                    debug["payload_to_member_id"] = _get_any(payload, "to_member_id", "toMemberId")
                    debug["payload_to_member_id_int"] = _get_any(payload, "to_member_id_int", "toMemberIdInt")
                    debug["payload_to_member_nickname"] = _get_any(payload, "to_member_nickname", "toMemberNickname")
                if event_to_user:
                    debug["event_to_user"] = {
                        "id": _get_attr_any(event_to_user, "id", "user_id"),
                        "unique_id": _extract_handle(event_to_user),
                        "nickname": _get_attr_any(event_to_user, "nickname", "nick_name", "nickName"),
                    }
                if base_message:
                    debug["base_message_id"] = _get_attr_any(base_message, "message_id", "messageId")
                    debug["base_message_room_id"] = _get_attr_any(base_message, "room_id", "roomId")
            except Exception:
                pass
            logger.info(
                "Gift still missing to_user fields after fallback: to_user_id=%s to_username=%s to_nickname=%s debug=%s",
                row.get("to_user_id"),
                row.get("to_username"),
                row.get("to_nickname"),
                _safe_json(debug),
            )
        elif not row.get("to_user_id"):
            logger.debug(
                "Gift missing to_user_id after fallback: to_username=%s to_nickname=%s",
                row.get("to_username"),
                row.get("to_nickname"),
            )
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping gift_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                on_conflict = "message_id" if row_data.get("message_id") else None
                action = "upsert" if on_conflict else "insert"
                resp = await self._post_row("gift_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("gift_events", row_data)
                    action = "insert-fallback"
                self._log_result("gift_events", action, resp)
            except Exception:
                logger.exception("Failed to log gift event")

    async def log_join(self, payload: dict, event, tiktok_username: str, raw_id: Optional[int] = None) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())

        base_message = _get_attr_any(event, "base_message", "baseMessage")
        user_obj = _safe_event_user(event)

        def _get_child(obj, *names):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return _get_any(obj, *names)
            return _get_attr_any(obj, *names)

        user_payload = payload.get("user") if isinstance(payload, dict) and isinstance(payload.get("user"), dict) else {}
        follow_info = _get_child(user_payload, "followInfo", "follow_info")
        if follow_info is None:
            follow_info = _get_attr_any(user_obj, "follow_info", "followInfo")
        if follow_info is None and isinstance(base_message, dict):
            display_text = _get_child(base_message, "displayText", "display_text")
            pieces = _get_child(display_text, "pieces")
            if isinstance(pieces, list):
                for piece in pieces:
                    piece_user = _get_child(_get_child(piece, "userValue", "user_value"), "user")
                    fi = _get_child(piece_user, "followInfo", "follow_info")
                    if fi is not None:
                        follow_info = fi
                        break

        joined_user_id = (
            _get_attr_any(user_obj, "id", "user_id", "userId")
            or _get_any(user_payload, "id", "user_id", "userId")
            or _get_any(payload, "joined_user_id", "joinedUserId")
        )
        joined_user_nickname = (
            _get_attr_any(user_obj, "nickname", "nick_name", "nickName")
            or _get_any(user_payload, "nickName", "nick_name", "nickname")
            or _get_any(payload, "joined_user_nickname", "joinedUserNickname")
        )
        joined_username = (
            _extract_handle(user_obj)
            or _get_any(user_payload, "unique_id", "display_id", "username")
            or _get_any(payload, "joined_username", "joinedUsername")
        )
        joined_user_sec_uid = (
            _get_attr_any(user_obj, "sec_uid", "secUid", "user_sec_uid")
            or _get_any(user_payload, "secUid", "sec_uid", "user_sec_uid")
            or _get_any(payload, "joined_user_sec_uid", "joinedUserSecUid")
        )

        action = _get_any(payload, "action") or _get_attr_any(event, "action")
        count = _get_any(payload, "count") or _get_attr_any(event, "count")
        client_enter_source = _get_any(payload, "clientEnterSource", "client_enter_source")
        client_enter_type = _get_any(payload, "clientEnterType", "client_enter_type")

        public_area = _get_child(payload, "publicAreaMessageCommon", "public_area_message_common")
        if public_area is None:
            public_area = _get_attr_any(event, "public_area_message_common", "publicAreaMessageCommon")
        if public_area is None and base_message is not None:
            public_area = _get_attr_any(base_message, "public_area_message_common", "publicAreaMessageCommon")

        portrait = _get_child(public_area, "portraitInfo", "portrait_info")
        user_metrics = _get_child(portrait, "userMetrics", "user_metrics")
        if not isinstance(user_metrics, list):
            user_metrics = []

        grade_metric = None
        subscribe_metric = None
        follow_metric = None
        fans_club_metric = None
        top_viewer_metric = None

        type_map_0_based = {
            0: "GRADE",
            1: "SUBSCRIBE",
            2: "FOLLOW",
            3: "FANS_CLUB",
            4: "TOP_VIEWER",
        }
        type_map_1_based = {
            1: "GRADE",
            2: "SUBSCRIBE",
            3: "FOLLOW",
            4: "FANS_CLUB",
            5: "TOP_VIEWER",
        }
        numeric_types = []
        normalized_metrics = []
        for metric in user_metrics:
            metric_type_raw = _get_child(metric, "type", "metricType", "metric_type")
            metric_val = _get_child(metric, "metricsValue", "metrics_value", "value")
            normalized_metrics.append((metric_type_raw, metric_val))
            numeric_type = self._to_int(metric_type_raw)
            if numeric_type is not None:
                numeric_types.append(numeric_type)
        enum_map = None
        type_set = set(numeric_types)
        if type_set.issubset(set(type_map_0_based.keys())) and type_set:
            enum_map = type_map_0_based
        elif type_set.issubset(set(type_map_1_based.keys())) and type_set:
            enum_map = type_map_1_based

        for metric_type_raw, metric_val in normalized_metrics:
            metric_type = str(metric_type_raw or "").upper()
            numeric_type = self._to_int(metric_type_raw)
            if (
                enum_map is not None
                and numeric_type in enum_map
                and (
                    not metric_type
                    or metric_type.isdigit()
                    or metric_type in {"UNKNOWN", "UNSPECIFIED", "NONE"}
                )
            ):
                metric_type = enum_map[numeric_type]
            metric_val_int = self._to_int(metric_val)
            metric_val_norm = metric_val_int if metric_val_int is not None else metric_val
            if "GRADE" in metric_type:
                grade_metric = metric_val_norm
            elif "SUBSCRIBE" in metric_type:
                subscribe_metric = metric_val_norm
            elif "FOLLOW" in metric_type:
                follow_metric = metric_val_norm
            elif "FANS_CLUB" in metric_type or "FANSCLUB" in metric_type:
                fans_club_metric = metric_val_norm
            elif "TOP_VIEWER" in metric_type:
                top_viewer_metric = metric_val_norm

        follow_role = _get_any(payload, "follow_role", "followRole")
        if not _is_valid_value(follow_role) and follow_info is not None:
            follow_role = _get_child(
                follow_info,
                "follow_role",
                "followRole",
                "follow_status",
                "followStatus",
                "follow_state",
                "followState",
            )
        if not _is_valid_value(follow_role):
            follow_role = follow_metric
        if not _is_valid_value(follow_role):
            follow_role = None

        following_count = _get_child(follow_info, "followingCount", "following_count")
        follower_count = _get_child(follow_info, "followerCount", "follower_count")
        if following_count is None:
            following_count = _get_child(user_payload, "followingCount", "following_count")
        if follower_count is None:
            follower_count = _get_child(user_payload, "followerCount", "follower_count")
        if following_count is None:
            following_count = _get_attr_any(user_obj, "following_count", "followingCount")
        if follower_count is None:
            follower_count = _get_attr_any(user_obj, "follower_count", "followerCount")
        following_count = self._to_int(following_count)
        follower_count = self._to_int(follower_count)

        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "method": _get_any(payload, "method") or _get_attr_any(base_message, "method") or "WebcastMemberMessage",
            "room_id": _get_any(payload, "room_id", "roomId") or _get_attr_any(base_message, "room_id", "roomId"),
            "create_time_ms": _get_any(payload, "create_time", "create_time_ms", "createTime", "timestamp")
            or _get_attr_any(base_message, "create_time", "createTime"),
            "message_id": _get_any(payload, "msg_id", "msgId", "message_id", "messageId", "event_id")
            or _get_attr_any(base_message, "message_id", "messageId"),
            "joined_user_id": joined_user_id,
            "joined_user_nickname": joined_user_nickname,
            "joined_username": joined_username,
            "joined_user_sec_uid": joined_user_sec_uid,
            "action": action,
            "count": count,
            "client_enter_source": client_enter_source,
            "client_enter_type": client_enter_type,
            "grade_metric": grade_metric,
            "subscribe_metric": subscribe_metric,
            "follow_metric": follow_metric,
            "fans_club_metric": fans_club_metric,
            "top_viewer_metric": top_viewer_metric,
            "follow_role": follow_role,
            "following_count": following_count,
            "follower_count": follower_count,
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping join_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                on_conflict = "message_id,tiktok_username" if row_data.get("message_id") else None
                action_name = "upsert" if on_conflict else "insert"
                resp = await self._post_row("join_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("join_events", row_data)
                    action_name = "insert-fallback"
                self._log_result("join_events", action_name, resp)
            except Exception:
                logger.exception("Failed to log join event")

    async def log_room_update(
        self,
        payload: dict,
        event,
        tiktok_username: str,
        raw_id: Optional[int] = None,
    ) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())

        def _get_child(obj, *names):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return _get_any(obj, *names)
            return _get_attr_any(obj, *names)

        base_message = _get_child(payload, "baseMessage", "base_message")
        if base_message is None:
            base_message = _get_attr_any(event, "base_message", "baseMessage")

        contributors = _get_child(payload, "mContributors", "m_contributors", "contributors")
        if contributors is None:
            contributors = _get_attr_any(event, "m_contributors", "mContributors", "contributors")
        if contributors is None and base_message is not None:
            contributors = _get_child(base_message, "mContributors", "m_contributors", "contributors")
        if not isinstance(contributors, list):
            contributors = []

        m_contributors = []
        for contributor in contributors:
            contributor_user = _get_child(contributor, "mUser", "m_user", "user")
            m_user_id = _get_child(contributor_user, "id", "idStr", "user_id", "userId")
            if m_user_id is not None:
                try:
                    m_user_id = str(m_user_id).strip()
                except Exception:
                    m_user_id = None
            m_contributors.append(
                {
                    "mScore": self._to_int(_get_child(contributor, "mScore", "m_score", "score")),
                    "mUserId": m_user_id,
                    "mUsername": _get_child(contributor_user, "username", "unique_id", "display_id", "userName"),
                    "mRank": self._to_int(_get_child(contributor, "mRank", "m_rank", "rank")),
                }
            )

        # Build rank lookup from contributor payload for rank_2..rank_5.
        rank_lookup = {}
        for item in m_contributors:
            rank_val = self._to_int(item.get("mRank"))
            if rank_val is None or rank_val < 1 or rank_val > 5:
                continue
            if rank_val not in rank_lookup:
                rank_lookup[rank_val] = item

        def _rank_value(item) -> int:
            rank = self._to_int(_get_child(item, "mRank", "m_rank", "rank"))
            if rank is None:
                return 10**9
            return rank

        top = None
        if contributors:
            top = min(contributors, key=_rank_value)
            if _rank_value(top) >= 10**9:
                top = contributors[0]
        top_user = _get_child(top, "mUser", "m_user", "user")

        top_id = _get_child(top_user, "id", "user_id", "userId", "idStr")
        top_username = _get_child(top_user, "username", "unique_id", "display_id", "userName")
        top_score = _get_child(top, "mScore", "m_score", "score")

        rank_1_fallback = rank_lookup.get(1, {})
        rank_1 = {
            "id": top_id if _is_valid_value(top_id) else rank_1_fallback.get("mUserId"),
            "username": top_username if _is_valid_value(top_username) else rank_1_fallback.get("mUsername"),
            "score": top_score if _is_valid_value(top_score) else rank_1_fallback.get("mScore"),
        }
        rank_2 = rank_lookup.get(2, {})
        rank_3 = rank_lookup.get(3, {})
        rank_4 = rank_lookup.get(4, {})
        rank_5 = rank_lookup.get(5, {})

        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "method": _get_child(payload, "method")
            or _get_child(base_message, "method")
            or _get_attr_any(event, "method")
            or "WebcastRoomUserSeqMessage",
            "room_id": _get_child(payload, "room_id", "roomId")
            or _get_child(base_message, "roomId", "room_id")
            or _get_attr_any(event, "room_id", "roomId"),
            "create_time_ms": _get_child(payload, "create_time", "create_time_ms", "createTime", "timestamp")
            or _get_child(base_message, "createTime", "create_time")
            or _get_attr_any(event, "create_time", "createTime", "timestamp"),
            "message_id": _get_child(payload, "msg_id", "msgId", "message_id", "messageId", "event_id")
            or _get_child(base_message, "messageId", "message_id")
            or _get_attr_any(event, "msg_id", "msgId", "message_id", "messageId", "event_id", "_ws_msg_id"),
            "viewer_count_m_total": _get_child(payload, "mTotal", "m_total", "total"),
            "viewer_count_total_user": _get_child(payload, "totalUser", "total_user"),
            "anonymous": _get_child(payload, "anonymous"),
            "contributors_count": len(m_contributors),
            "rank_1_contributor_id": rank_1.get("id"),
            "rank_1_contributor_username": rank_1.get("username"),
            "rank_1_contributor_score": rank_1.get("score"),
            "rank_2_contributor_id": rank_2.get("mUserId"),
            "rank_2_contributor_username": rank_2.get("mUsername"),
            "rank_2_contributor_score": rank_2.get("mScore"),
            "rank_3_contributor_id": rank_3.get("mUserId"),
            "rank_3_contributor_username": rank_3.get("mUsername"),
            "rank_3_contributor_score": rank_3.get("mScore"),
            "rank_4_contributor_id": rank_4.get("mUserId"),
            "rank_4_contributor_username": rank_4.get("mUsername"),
            "rank_4_contributor_score": rank_4.get("mScore"),
            "rank_5_contributor_id": rank_5.get("mUserId"),
            "rank_5_contributor_username": rank_5.get("mUsername"),
            "rank_5_contributor_score": rank_5.get("mScore"),
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping room_update_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                # Ensure numeric text is normalized for bigint/integer columns when present.
                for key in (
                    "room_id",
                    "create_time_ms",
                    "message_id",
                    "viewer_count_m_total",
                    "viewer_count_total_user",
                    "anonymous",
                    "rank_1_contributor_id",
                    "rank_1_contributor_score",
                    "rank_2_contributor_id",
                    "rank_2_contributor_score",
                    "rank_3_contributor_id",
                    "rank_3_contributor_score",
                    "rank_4_contributor_id",
                    "rank_4_contributor_score",
                    "rank_5_contributor_id",
                    "rank_5_contributor_score",
                ):
                    if key in row_data:
                        coerced = self._to_int(row_data.get(key))
                        if coerced is not None:
                            row_data[key] = coerced

                partition_key = self._room_update_partition_key(row_data)
                state_tuple = self._room_update_state_tuple(row_data)
                if self._room_update_last_state.get(partition_key) == state_tuple:
                    logger.debug(
                        "Skipping unchanged room_update snapshot for @%s room_id=%s message_id=%s",
                        row_data.get("tiktok_username"),
                        row_data.get("room_id"),
                        row_data.get("message_id"),
                    )
                    return

                resp = await self._post_row("room_update_events", row_data)
                self._log_result("room_update_events", "insert", resp)
                if resp is not None and resp.is_success:
                    self._room_update_last_state[partition_key] = state_tuple
            except Exception:
                logger.exception("Failed to log room update event")

    async def log_goal_update(
        self,
        payload: dict,
        event,
        tiktok_username: str,
        raw_id: Optional[int] = None,
    ) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())

        def _get_child(obj, *names):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return _get_any(obj, *names)
            return _get_attr_any(obj, *names)

        def _as_jsonable(obj, default):
            if obj is None:
                return default
            if isinstance(obj, (dict, list)):
                return obj
            for method in ("to_dict", "as_dict"):
                try:
                    fn = getattr(obj, method, None)
                    if callable(fn):
                        converted = fn()
                        if isinstance(converted, (dict, list)):
                            return converted
                except Exception:
                    pass
            try:
                converted = json.loads(_safe_json(obj))
                if isinstance(converted, (dict, list)):
                    return converted
            except Exception:
                pass
            return default

        ws_payload = getattr(event, "_ws_message_dict", None)
        if not isinstance(ws_payload, dict):
            ws_payload = {}

        base_message = _get_child(payload, "baseMessage", "base_message")
        if base_message is None:
            base_message = _get_child(ws_payload, "baseMessage", "base_message")
        if base_message is None:
            base_message = _get_attr_any(event, "base_message", "baseMessage")
        base_message_json = _as_jsonable(base_message, {})

        indicator = _get_child(payload, "indicator")
        if indicator is None:
            indicator = _get_child(ws_payload, "indicator")
        if indicator is None:
            indicator = _get_attr_any(event, "indicator")
        indicator_json = _as_jsonable(indicator, {})

        goal = _get_child(payload, "goal")
        if goal is None:
            goal = _get_child(ws_payload, "goal")
        if goal is None:
            goal = _get_attr_any(event, "goal")
        goal_json = _as_jsonable(goal, {})

        subgoals = _get_child(goal_json, "subGoals", "sub_goals")
        if isinstance(subgoals, tuple):
            subgoals = list(subgoals)
        if not isinstance(subgoals, list):
            subgoals = []
        if not subgoals:
            contribute_subgoal = _get_child(payload, "contributeSubgoal", "contribute_subgoal")
            if contribute_subgoal is None:
                contribute_subgoal = _get_child(ws_payload, "contributeSubgoal", "contribute_subgoal")
            if contribute_subgoal is None:
                contribute_subgoal = _get_attr_any(event, "contribute_subgoal", "contributeSubgoal")
            contribute_subgoal_json = _as_jsonable(contribute_subgoal, {})
            if isinstance(contribute_subgoal_json, dict) and contribute_subgoal_json:
                subgoals = [contribute_subgoal_json]

        subgoal_0 = subgoals[0] if subgoals else {}
        if not isinstance(subgoal_0, dict):
            subgoal_0 = _as_jsonable(subgoal_0, {})
        subgoal_0_gift = _get_child(subgoal_0, "gift")
        if not isinstance(subgoal_0_gift, dict):
            subgoal_0_gift = _as_jsonable(subgoal_0_gift, {})

        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "method": _get_child(payload, "method")
            or _get_child(ws_payload, "method")
            or _get_child(base_message_json, "method")
            or _get_attr_any(event, "method")
            or "WebcastGoalUpdateMessage",
            "room_id": _get_child(payload, "room_id", "roomId")
            or _get_child(ws_payload, "room_id", "roomId")
            or _get_child(base_message_json, "roomId", "room_id")
            or _get_attr_any(event, "room_id", "roomId"),
            "create_time_ms": _get_child(payload, "create_time", "create_time_ms", "createTime", "timestamp")
            or _get_child(ws_payload, "create_time", "create_time_ms", "createTime", "timestamp")
            or _get_child(base_message_json, "createTime", "create_time")
            or _get_attr_any(event, "create_time", "createTime", "timestamp"),
            "message_id": _get_child(payload, "msg_id", "msgId", "message_id", "messageId", "event_id")
            or _get_child(ws_payload, "msg_id", "msgId", "message_id", "messageId", "event_id")
            or _get_child(base_message_json, "messageId", "message_id")
            or _get_attr_any(event, "msg_id", "msgId", "message_id", "messageId", "event_id", "_ws_msg_id"),
            "goal_id": _get_child(goal_json, "id"),
            "goal_id_str": _get_child(goal_json, "idStr", "id_str"),
            "goal_description": _get_child(goal_json, "description"),
            "goal_type": _get_child(goal_json, "type"),
            "goal_status": _get_child(goal_json, "status"),
            "goal_audit_status": _get_child(goal_json, "auditStatus", "audit_status"),
            "goal_cycle_type": _get_child(goal_json, "cycleType", "cycle_type"),
            "goal_start_time": _get_child(goal_json, "startTime", "start_time"),
            "goal_expire_time": _get_child(goal_json, "expireTime", "expire_time"),
            "goal_real_finish_time": _get_child(goal_json, "realFinishTime", "real_finish_time"),
            "goal_contributors_length": _get_child(goal_json, "contributorsLength", "contributors_length")
            or (len(_get_child(goal_json, "contributors") or []) if isinstance(_get_child(goal_json, "contributors"), list) else None),
            "goal_audit_description": _get_child(goal_json, "auditDescription", "audit_description")
            or _get_child(goal_json, "description"),
            "goal_challenge_type": _get_child(goal_json, "challengeType", "challenge_type"),
            "contributor_id": _get_child(payload, "contributorId", "contributor_id")
            or _get_child(ws_payload, "contributorId", "contributor_id"),
            "contributor_id_str": _get_child(payload, "contributorIdStr", "contributor_id_str")
            or _get_child(ws_payload, "contributorIdStr", "contributor_id_str"),
            "contributor_display_id": _get_child(payload, "contributorDisplayId", "contributor_display_id")
            or _get_child(ws_payload, "contributorDisplayId", "contributor_display_id"),
            "contribute_count": _get_child(payload, "contributeCount", "contribute_count")
            or _get_child(ws_payload, "contributeCount", "contribute_count"),
            "contribute_score": _get_child(payload, "contributeScore", "contribute_score")
            or _get_child(ws_payload, "contributeScore", "contribute_score"),
            "gift_repeat_count": _get_child(payload, "giftRepeatCount", "gift_repeat_count")
            or _get_child(ws_payload, "giftRepeatCount", "gift_repeat_count"),
            "update_source": _get_child(payload, "updateSource", "update_source")
            or _get_child(ws_payload, "updateSource", "update_source"),
            "goal_extra": _get_child(payload, "goalExtra", "goal_extra")
            or _get_child(ws_payload, "goalExtra", "goal_extra"),
            "subgoals_count": len(subgoals),
            "subgoal_0_id": _get_child(subgoal_0, "id"),
            "subgoal_0_target": _get_child(subgoal_0, "target"),
            "subgoal_0_progress": _get_child(subgoal_0, "progress")
            or _get_child(payload, "contributeCount", "contribute_count")
            or _get_child(ws_payload, "contributeCount", "contribute_count"),
            "subgoal_0_gift_name": _get_child(subgoal_0_gift, "name"),
            "subgoal_0_gift_diamond_count": _get_child(subgoal_0_gift, "diamondCount", "diamond_count"),
            "indicator_key": _get_child(indicator_json, "key"),
            "indicator_op": _get_child(indicator_json, "op"),
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping goal_update_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                # Normalize known numeric fields for bigint/int columns.
                for key in (
                    "room_id",
                    "create_time_ms",
                    "message_id",
                    "goal_id",
                    "goal_id_str",
                    "goal_type",
                    "goal_status",
                    "goal_cycle_type",
                    "goal_start_time",
                    "goal_expire_time",
                    "goal_real_finish_time",
                    "goal_contributors_length",
                    "goal_challenge_type",
                    "contributor_id",
                    "contributor_id_str",
                    "contribute_count",
                    "contribute_score",
                    "gift_repeat_count",
                    "subgoals_count",
                    "subgoal_0_id",
                    "subgoal_0_target",
                    "subgoal_0_progress",
                    "subgoal_0_gift_diamond_count",
                    "indicator_op",
                ):
                    if key in row_data:
                        coerced = self._to_int(row_data.get(key))
                        if coerced is not None:
                            row_data[key] = coerced
                on_conflict = "message_id,tiktok_username" if row_data.get("message_id") else None
                action_name = "upsert" if on_conflict else "insert"
                resp = await self._post_row("goal_update_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("goal_update_events", row_data)
                    action_name = "insert-fallback"
                self._log_result("goal_update_events", action_name, resp)
            except Exception:
                logger.exception("Failed to log goal update event")

    async def log_social(
        self,
        payload: dict,
        event,
        tiktok_username: str,
        raw_id: Optional[int] = None,
    ) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())

        def _get_child(obj, *names):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return _get_any(obj, *names)
            return _get_attr_any(obj, *names)

        def _as_jsonable(obj, default):
            if obj is None:
                return default
            if isinstance(obj, (dict, list)):
                return obj
            for method in ("to_dict", "as_dict"):
                try:
                    fn = getattr(obj, method, None)
                    if callable(fn):
                        converted = fn()
                        if isinstance(converted, (dict, list)):
                            return converted
                except Exception:
                    pass
            try:
                converted = json.loads(_safe_json(obj))
                if isinstance(converted, (dict, list)):
                    return converted
            except Exception:
                pass
            return default

        base_message = _get_child(payload, "baseMessage", "base_message")
        if base_message is None:
            base_message = _get_attr_any(event, "base_message", "baseMessage")

        user = _get_child(payload, "user")
        if user is None:
            user = _safe_event_user(event) or _get_attr_any(event, "user", "from_user", "fromUser")
        if user is None and base_message is not None:
            display_text_from_base = _get_child(base_message, "displayText", "display_text")
            pieces = _get_child(display_text_from_base, "pieces")
            if isinstance(pieces, list):
                for piece in pieces:
                    piece_user = _get_child(_get_child(piece, "userValue", "user_value"), "user")
                    if piece_user is not None:
                        user = piece_user
                        break

        public_area = _get_child(payload, "publicAreaMessageCommon", "public_area_message_common")
        if public_area is None:
            public_area = _get_attr_any(event, "public_area_message_common", "publicAreaMessageCommon")
        if public_area is None:
            public_area = _get_child(base_message, "publicAreaMessageCommon", "public_area_message_common")
        public_area_json = _as_jsonable(public_area, {})

        portrait = _get_child(public_area_json, "portraitInfo", "portrait_info")
        portrait_json = _as_jsonable(portrait, {})
        portrait_tags = _get_child(portrait_json, "portraitTag", "portrait_tag")
        if isinstance(portrait_tags, tuple):
            portrait_tags = list(portrait_tags)
        if not isinstance(portrait_tags, list):
            portrait_tags = []
        portrait_tag_0 = portrait_tags[0] if portrait_tags else None
        portrait_tag_0_json = _as_jsonable(portrait_tag_0, None)

        display_text = _get_child(base_message, "displayText", "display_text")
        if display_text is None:
            display_text = _get_child(payload, "displayText", "display_text")
        display_text_json = _as_jsonable(display_text, {})
        display_key = str(_get_child(display_text_json, "key") or "")
        default_pattern = str(_get_child(display_text_json, "defaultPattern", "default_pattern") or "")
        lower_key = display_key.lower()
        lower_pattern = default_pattern.lower()
        action_raw = _get_child(payload, "action") or _get_attr_any(event, "action")
        share_type_raw = _get_child(payload, "shareType", "share_type") or _get_attr_any(event, "share_type", "shareType")
        share_type_text = str(share_type_raw).lower() if share_type_raw is not None else ""
        show_duration_ms_raw = (
            _get_child(payload, "showDurationMs", "show_duration_ms", "showDuration", "show_duration")
            or _get_child(base_message, "showDurationMs", "show_duration_ms", "showDuration", "show_duration")
            or _get_child(display_text_json, "showDurationMs", "show_duration_ms", "showDuration", "show_duration")
            or _get_attr_any(event, "show_duration_ms", "showDurationMs", "show_duration", "showDuration")
        )
        show_duration_ms_value = self._to_int(show_duration_ms_raw)
        if show_duration_ms_value is None:
            # Not all Social payloads include duration; use 0 instead of null for consistency.
            show_duration_ms_value = 0
        if "repost" in lower_key or "reposted" in lower_pattern or "repost" in share_type_text:
            social_type = "repost"
        elif "follow" in lower_key or "followed" in lower_pattern:
            social_type = "follow"
        elif "share" in lower_key or "shared" in lower_pattern:
            social_type = "share"
        else:
            social_type = default_pattern or display_key or (f"action_{action_raw}" if action_raw is not None else "unknown")

        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "method": _get_child(payload, "method")
            or _get_child(base_message, "method")
            or _get_attr_any(event, "method")
            or "WebcastSocialMessage",
            "message_id": _get_child(payload, "msg_id", "msgId", "message_id", "messageId", "event_id")
            or _get_child(base_message, "messageId", "message_id")
            or _get_attr_any(event, "msg_id", "msgId", "message_id", "messageId", "event_id", "_ws_msg_id"),
            "room_id": _get_child(payload, "room_id", "roomId")
            or _get_child(base_message, "roomId", "room_id")
            or _get_attr_any(event, "room_id", "roomId"),
            "create_time_ms": _get_child(payload, "create_time", "create_time_ms", "createTime", "timestamp")
            or _get_child(base_message, "createTime", "create_time")
            or _get_attr_any(event, "create_time", "createTime", "timestamp"),
            "user_id": _get_child(user, "id", "userId", "user_id", "idStr"),
            "user_nickname": _get_child(user, "nickName", "nick_name", "nickname"),
            "user_username": _get_child(user, "username", "unique_id", "display_id") or _extract_handle(user),
            "user_sec_uid": _get_child(user, "secUid", "sec_uid", "user_sec_uid"),
            "action": action_raw,
            "share_type": share_type_raw,
            "share_target": _get_child(payload, "shareTarget", "share_target") or _get_attr_any(event, "share_target", "shareTarget"),
            "follow_count": _get_child(payload, "followCount", "follow_count") or _get_attr_any(event, "follow_count", "followCount"),
            "share_count": _get_child(payload, "shareCount", "share_count") or _get_attr_any(event, "share_count", "shareCount"),
            "signature": _get_child(payload, "signature") or _get_attr_any(event, "signature"),
            "signature_version": _get_child(payload, "signatureVersion", "signature_version")
            or _get_attr_any(event, "signature_version", "signatureVersion"),
            "show_duration_ms": show_duration_ms_value,
            "social_type": social_type,
            "portrait_tags_count": len(portrait_tags),
            "portrait_tag_0": portrait_tag_0_json,
            "public_area_message_common": public_area_json,
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping social_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                # Keep json/jsonb fields as native JSON for PostgREST.
                row_data["portrait_tag_0"] = portrait_tag_0_json
                row_data["public_area_message_common"] = public_area_json
                # Normalize known numeric fields for bigint/int columns.
                for key in (
                    "message_id",
                    "room_id",
                    "create_time_ms",
                    "user_id",
                    "action",
                    "share_type",
                    "share_target",
                    "follow_count",
                    "share_count",
                    "show_duration_ms",
                    "portrait_tags_count",
                ):
                    if key in row_data:
                        coerced = self._to_int(row_data.get(key))
                        if coerced is not None:
                            row_data[key] = coerced
                on_conflict = "message_id,tiktok_username" if row_data.get("message_id") else None
                action_name = "upsert" if on_conflict else "insert"
                resp = await self._post_row("social_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("social_events", row_data)
                    action_name = "insert-fallback"
                self._log_result("social_events", action_name, resp)
            except Exception:
                logger.exception("Failed to log social event")

    async def log_comment(self, payload: dict, event, tiktok_username: str, raw_id: Optional[int] = None) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())
        base_message = _get_attr_any(event, "base_message", "baseMessage")
        user_info = _get_attr_any(event, "user_info", "userInfo")
        comment_text = ""
        try:
            comment_text = getattr(event, "comment", "") or getattr(event, "content", "") or ""
        except Exception:
            comment_text = ""
        if not comment_text and isinstance(payload, dict):
            comment_text = _get_any(payload, "comment", "content", default="")
        user_obj = _safe_event_user(event)
        username = _extract_handle(user_obj)
        nickname = getattr(user_obj, "nickname", "") if user_obj else ""
        user_id = getattr(user_obj, "unique_id", "") if user_obj else ""
        user_sec_uid = ""
        if isinstance(payload, dict):
            user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else {}
            username = username or _get_any(user_payload, "unique_id", "display_id", "username")
            nickname = nickname or _get_any(user_payload, "nickname", "nick_name")
            user_id = user_id or _get_any(user_payload, "id", "user_id")
            user_sec_uid = _get_any(user_payload, "sec_uid", "user_sec_uid")
        follow_role = _get_any(payload, "follow_role", "followRole")
        if isinstance(payload, dict):
            user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else {}
            follow_info = user_payload.get("follow_info") if isinstance(user_payload.get("follow_info"), dict) else None
            if follow_info is None and isinstance(user_payload.get("followInfo"), dict):
                follow_info = user_payload.get("followInfo")
            follow_role = follow_role or _get_any(
                user_payload, "follow_role", "followRole", "follow_status", "followStatus", "follow_state", "followState"
            )
            if follow_info:
                follow_role = follow_role or _get_any(
                    follow_info, "follow_role", "followRole", "follow_status", "followStatus", "follow_state", "followState"
                )
        if user_info:
            username = username or _extract_handle(user_info)
            nickname = nickname or _get_attr_any(user_info, "nickname", "nick_name", "nickName")
            user_id = user_id or _get_attr_any(user_info, "id", "user_id")
            user_sec_uid = user_sec_uid or _get_attr_any(user_info, "sec_uid", "user_sec_uid")
            follow_role = follow_role or _get_attr_any(user_info, "follow_role", "followRole", "follow_status", "followStatus")
            follow_info_obj = _get_attr_any(user_info, "follow_info", "followInfo")
            follow_role = follow_role or _get_attr_any(
                follow_info_obj, "follow_role", "followRole", "follow_status", "followStatus"
            )
        if not _is_valid_value(follow_role):
            follow_role = None
        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "event_type": "comment",
            "room_id": _get_any(payload, "room_id", "roomId") or _get_attr_any(base_message, "room_id", "roomId"),
            "create_time_ms": _get_any(payload, "create_time", "create_time_ms", "timestamp") or _get_attr_any(
                base_message, "create_time", "createTime"
            ),
            "message_id": _get_any(payload, "msg_id", "message_id", "event_id") or _get_attr_any(
                base_message, "message_id", "messageId"
            ),
            "comment_text": comment_text,
            "user_id": user_id,
            "username": username,
            "nickname": nickname,
            "user_sec_uid": user_sec_uid or _get_any(payload, "user_sec_uid", "sec_uid"),
            "follow_role": follow_role,
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping comment_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                on_conflict = "message_id" if row_data.get("message_id") else None
                action = "upsert" if on_conflict else "insert"
                resp = await self._post_row("comment_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("comment_events", row_data)
                    action = "insert-fallback"
                self._log_result("comment_events", action, resp)
            except Exception:
                logger.exception("Failed to log comment event")

    async def log_like(self, payload: dict, event, tiktok_username: str, raw_id: Optional[int] = None) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())
        user_obj = _safe_event_user(event)
        username = _extract_handle(user_obj)
        nickname = getattr(user_obj, "nickname", "") if user_obj else ""
        user_id = getattr(user_obj, "unique_id", "") if user_obj else ""
        user_sec_uid = _get_attr_any(user_obj, "sec_uid", "user_sec_uid")
        follow_role = _get_attr_any(user_obj, "follow_role", "followRole")
        if isinstance(payload, dict):
            user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else {}
            username = username or _get_any(user_payload, "unique_id", "display_id", "username")
            nickname = nickname or _get_any(user_payload, "nickname", "nick_name")
            user_id = user_id or _get_any(user_payload, "id", "user_id")
            user_sec_uid = user_sec_uid or _get_any(user_payload, "sec_uid", "user_sec_uid")
        if not username or not user_id:
            user_info = _get_attr_any(event, "user_info", "userInfo")
            if user_info:
                username = username or _extract_handle(user_info)
                nickname = nickname or _get_attr_any(user_info, "nickname", "nick_name", "nickName")
                user_id = user_id or _get_attr_any(user_info, "id", "user_id")
                user_sec_uid = user_sec_uid or _get_attr_any(user_info, "sec_uid", "user_sec_uid")
        base_message = _get_attr_any(event, "base_message", "baseMessage")
        room_id = _get_any(payload, "room_id", "roomId") or _get_attr_any(base_message, "room_id", "roomId")
        create_time_ms = _get_any(payload, "create_time", "create_time_ms", "timestamp") or _get_attr_any(
            base_message, "create_time", "createTime"
        )
        message_id = _get_any(payload, "msg_id", "message_id", "event_id") or _get_attr_any(
            base_message, "message_id", "messageId"
        )
        like_count = _get_any(payload, "like_count", "count", "likeCount") or _get_attr_any(
            event, "count", "like_count", "likeCount"
        )
        total_like_count = _get_any(payload, "total_like_count", "totalCount", "total_like") or _get_attr_any(
            event, "total", "total_like_count", "totalCount", "total_like"
        )
        user_sec_uid = user_sec_uid or _get_any(payload, "user_sec_uid", "sec_uid")
        follow_role = follow_role or _get_any(payload, "follow_role", "followRole")
        if isinstance(payload, dict):
            user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else {}
            user_sec_uid = user_sec_uid or _get_any(user_payload, "sec_uid", "user_sec_uid")
            follow_info = user_payload.get("follow_info") if isinstance(user_payload.get("follow_info"), dict) else None
            if follow_info is None and isinstance(user_payload.get("followInfo"), dict):
                follow_info = user_payload.get("followInfo")
            follow_role = follow_role or _get_any(
                user_payload, "follow_role", "followRole", "follow_status", "followStatus", "follow_state", "followState"
            )
            if follow_info:
                follow_role = follow_role or _get_any(
                    follow_info, "follow_role", "followRole", "follow_status", "followStatus", "follow_state", "followState"
                )
        if not user_sec_uid or not follow_role:
            user_info = _get_attr_any(event, "user_info", "userInfo")
            if user_info:
                user_sec_uid = user_sec_uid or _get_attr_any(user_info, "sec_uid", "user_sec_uid")
                follow_role = follow_role or _get_attr_any(user_info, "follow_role", "followRole", "follow_status", "followStatus")
                follow_info_obj = _get_attr_any(user_info, "follow_info", "followInfo")
                follow_role = follow_role or _get_attr_any(
                    follow_info_obj, "follow_role", "followRole", "follow_status", "followStatus"
                )
        if not follow_role:
            public_area = None
            if isinstance(payload, dict):
                public_area = payload.get("public_area_message_common") or payload.get("publicAreaMessageCommon")
            if public_area is None:
                public_area = _get_attr_any(event, "public_area_message_common", "publicAreaMessageCommon")
            if public_area is None and base_message:
                public_area = _get_attr_any(base_message, "public_area_message_common", "publicAreaMessageCommon")

            def _extract_follow_from_metrics(pa_obj):
                if not pa_obj:
                    return None
                portrait = pa_obj.get("portrait_info") if isinstance(pa_obj, dict) else _get_attr_any(pa_obj, "portrait_info", "portraitInfo")
                metrics = None
                if isinstance(portrait, dict):
                    metrics = portrait.get("user_metrics") or portrait.get("userMetrics")
                else:
                    metrics = _get_attr_any(portrait, "user_metrics", "userMetrics")
                if not metrics:
                    return None
                for metric in metrics:
                    m_type = metric.get("type") if isinstance(metric, dict) else _get_attr_any(metric, "type")
                    m_value = metric.get("metrics_value") if isinstance(metric, dict) else _get_attr_any(metric, "metrics_value", "metricsValue")
                    if m_type is None:
                        continue
                    if "FOLLOW" in str(m_type).upper():
                        return m_value
                return None

            follow_role = follow_role or _extract_follow_from_metrics(public_area)
            if not follow_role:
                portrait = public_area.get("portrait_info") if isinstance(public_area, dict) else _get_attr_any(public_area, "portrait_info", "portraitInfo")
                tags = portrait.get("portrait_tag") if isinstance(portrait, dict) else _get_attr_any(portrait, "portrait_tag", "portraitTag")
                if tags:
                    for tag in tags:
                        show_val = tag.get("show_value") if isinstance(tag, dict) else _get_attr_any(tag, "show_value", "showValue")
                        if not show_val:
                            continue
                        lower_val = str(show_val).lower()
                        if "notfollower" in lower_val or "not_follower" in lower_val:
                            follow_role = 0
                            break
                        if "follower" in lower_val:
                            follow_role = 1
                            break
        if not _is_valid_value(follow_role):
            follow_role = None
        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "event_type": "like",
            "room_id": room_id,
            "create_time_ms": create_time_ms,
            "message_id": message_id,
            "like_count": like_count,
            "total_like_count": total_like_count,
            "user_id": user_id,
            "username": username,
            "nickname": nickname,
            "user_sec_uid": user_sec_uid,
            "follow_role": follow_role,
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping like_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                on_conflict = "message_id" if row_data.get("message_id") else None
                action = "upsert" if on_conflict else "insert"
                resp = await self._post_row("like_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("like_events", row_data)
                    action = "insert-fallback"
                self._log_result("like_events", action, resp)
            except Exception:
                logger.exception("Failed to log like event")

    async def log_fan_event(self, payload: dict, event, tiktok_username: str, raw_id: Optional[int] = None) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())

        def _get_child(obj, *names):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return _get_any(obj, *names)
            return _get_attr_any(obj, *names)

        def _is_missing(value) -> bool:
            if value is None:
                return True
            if isinstance(value, bool):
                return True
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return True
                return _is_placeholder_obj(text)
            if isinstance(value, (dict, list, tuple, set)):
                return len(value) == 0
            try:
                text = str(value).strip()
            except Exception:
                return True
            if not text:
                return True
            return _is_placeholder_obj(text)

        def _first_int(*values) -> Optional[int]:
            for value in values:
                if _is_missing(value):
                    continue
                coerced = self._to_int(value)
                if coerced is not None:
                    return coerced
            return None

        def _first_text(*values) -> Optional[str]:
            for value in values:
                if _is_missing(value):
                    continue
                normalized = _normalize_db_value(value)
                if _is_missing(normalized):
                    continue
                return str(normalized).strip()
            return None

        def _as_dict(obj) -> Optional[dict]:
            if obj is None:
                return None
            if isinstance(obj, dict):
                return obj
            for method_name in ("to_dict", "as_dict"):
                try:
                    fn = getattr(obj, method_name, None)
                    if callable(fn):
                        converted = fn()
                        if isinstance(converted, dict):
                            return converted
                except Exception:
                    pass
            try:
                converted = vars(obj)
                if isinstance(converted, dict):
                    return converted
            except Exception:
                pass
            return None

        def _as_json_text(obj) -> Optional[str]:
            if obj is None:
                return None
            if isinstance(obj, (dict, list)):
                return _safe_json(obj)
            for method_name in ("to_dict", "as_dict"):
                try:
                    fn = getattr(obj, method_name, None)
                    if callable(fn):
                        converted = fn()
                        if isinstance(converted, (dict, list)):
                            return _safe_json(converted)
                except Exception:
                    pass
            try:
                parsed = json.loads(_safe_json(obj))
                if isinstance(parsed, (dict, list)):
                    return _safe_json(parsed)
            except Exception:
                pass
            return _normalize_db_value(obj)

        base_message = _get_child(payload, "base_message", "baseMessage")
        if base_message is None:
            base_message = _get_attr_any(event, "base_message", "baseMessage")

        user_obj = _safe_event_user(event) or _get_attr_any(event, "from_user", "fromUser")
        user_payload = _get_child(payload, "user", "from_user", "fromUser") if isinstance(payload, dict) else None
        if user_obj is None:
            user_obj = user_payload
        if user_obj is None:
            display_text = _get_child(base_message, "display_text", "displayText")
            pieces = _get_child(display_text, "pieces")
            if isinstance(pieces, list):
                for piece in pieces:
                    piece_user = _get_child(_get_child(piece, "user_value", "userValue"), "user")
                    if piece_user is not None:
                        user_obj = piece_user
                        break
        if user_payload is None:
            user_payload = _as_dict(user_obj)
        if not isinstance(user_payload, dict):
            user_payload = {}

        event_type_obj = _get_child(payload, "event_type", "eventType")
        if event_type_obj is None:
            event_type_obj = _get_attr_any(event, "event_type", "eventType")
        event_type_text = _first_text(
            _get_attr_any(event_type_obj, "name", "key"),
            event_type_obj,
            _get_child(payload, "event_type_name", "eventTypeName"),
        )

        event_type_value = _first_int(
            _get_attr_any(event_type_obj, "value", "number", "id"),
            _get_child(payload, "event_type_value", "eventTypeValue"),
            event_type_obj,
        )

        fans_level_info = _get_child(payload, "fans_level_info", "fansLevelInfo")
        if fans_level_info is None:
            fans_level_info = _get_attr_any(event, "fans_level_info", "fansLevelInfo")

        fans_level_upgrade_info = _get_child(payload, "fans_level_upgrade_info", "fansLevelUpgradeInfo")
        if fans_level_upgrade_info is None:
            fans_level_upgrade_info = _get_attr_any(event, "fans_level_upgrade_info", "fansLevelUpgradeInfo")

        data_obj = _get_child(payload, "data")
        if data_obj is None:
            data_obj = _get_attr_any(event, "data")

        fans_level_badge = _get_child(fans_level_info, "badge", "fans_badge", "fansBadge")
        new_fans_data = _get_child(data_obj, "new_fans_data", "newFansData")
        exp_change_data = _get_child(data_obj, "exp_change_data", "expChangeData")

        fans_level = _first_int(
            _get_child(fans_level_info, "level", "fans_level", "fansLevel", "current_level", "currentLevel"),
            _get_child(fans_level_upgrade_info, "level", "current_level", "currentLevel"),
            _get_child(new_fans_data, "level", "fans_level", "fansLevel"),
        )
        fans_level_name = _first_text(
            _get_child(fans_level_info, "level_name", "levelName", "name", "fans_level_name", "fansLevelName"),
            _get_child(fans_level_badge, "name", "title", "level_name", "levelName", "badge_name", "badgeName"),
            _get_child(new_fans_data, "level_name", "levelName", "name"),
        )
        upgrade_from_level = _first_int(
            _get_child(
                fans_level_upgrade_info,
                "from_level",
                "fromLevel",
                "old_level",
                "oldLevel",
                "before_level",
                "beforeLevel",
                "prev_level",
                "prevLevel",
            ),
            _get_child(exp_change_data, "from_level", "fromLevel", "old_level", "oldLevel"),
        )
        upgrade_to_level = _first_int(
            _get_child(
                fans_level_upgrade_info,
                "to_level",
                "toLevel",
                "new_level",
                "newLevel",
                "after_level",
                "afterLevel",
                "level",
                "current_level",
                "currentLevel",
            ),
            _get_child(exp_change_data, "to_level", "toLevel", "new_level", "newLevel", "level"),
        )

        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "event_type": "fans",
            "method": _first_text(
                _get_child(payload, "method"),
                _get_child(base_message, "method"),
                _get_attr_any(event, "method"),
                "WebcastFansEventMessage",
            ),
            "room_id": _first_int(
                _get_child(payload, "room_id", "roomId"),
                _get_child(base_message, "room_id", "roomId"),
                _get_attr_any(event, "room_id", "roomId"),
                _get_child(fans_level_info, "room_id", "roomId"),
            ),
            "create_time_ms": _first_int(
                _get_child(payload, "create_time", "create_time_ms", "createTime", "timestamp"),
                _get_child(base_message, "create_time", "createTime"),
                _get_attr_any(event, "create_time", "createTime", "timestamp"),
                int(unix_ts * 1000),
            ),
            "message_id": _first_int(
                _get_child(payload, "msg_id", "msgId", "message_id", "messageId", "event_id"),
                _get_child(base_message, "message_id", "messageId"),
                _get_attr_any(event, "msg_id", "msgId", "message_id", "messageId", "_ws_msg_id"),
            ),
            "user_id": _first_int(
                _get_attr_any(user_obj, "id", "user_id", "userId", "id_str", "idStr"),
                _get_any(user_payload, "id", "user_id", "userId", "id_str", "idStr"),
                _get_child(fans_level_info, "userid", "user_id", "userId"),
                _get_child(fans_level_upgrade_info, "user_id", "userId"),
                _get_child(new_fans_data, "user_id", "userId", "uid"),
            ),
            "username": _first_text(
                _extract_handle(user_obj),
                _extract_handle(user_payload),
                _get_any(user_payload, "username", "unique_id", "display_id", "user_name", "userName"),
                _get_child(new_fans_data, "username", "unique_id", "display_id", "user_name", "userName"),
            ),
            "nickname": _first_text(
                _get_attr_any(user_obj, "nick_name", "nickname", "nickName"),
                _get_any(user_payload, "nick_name", "nickname", "nickName"),
                _get_child(new_fans_data, "nick_name", "nickname", "nickName"),
            ),
            "user_sec_uid": _first_text(
                _get_attr_any(user_obj, "sec_uid", "secUid", "user_sec_uid"),
                _get_any(user_payload, "sec_uid", "secUid", "user_sec_uid"),
                _get_child(new_fans_data, "sec_uid", "secUid", "user_sec_uid"),
            ),
            "fans_event_type": event_type_text,
            "fans_event_type_value": event_type_value,
            "fans_level": fans_level,
            "fans_level_name": fans_level_name,
            "upgrade_from_level": upgrade_from_level,
            "upgrade_to_level": upgrade_to_level,
            "fans_level_info_json": _as_json_text(fans_level_info),
            "fans_level_upgrade_info_json": _as_json_text(fans_level_upgrade_info),
            "data_json": _as_json_text(data_obj),
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping fan_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                for key in (
                    "room_id",
                    "create_time_ms",
                    "message_id",
                    "fans_event_type_value",
                    "fans_level",
                    "upgrade_from_level",
                    "upgrade_to_level",
                ):
                    row_data[key] = self._to_int(row_data.get(key))
                on_conflict = "message_id,tiktok_username" if row_data.get("message_id") else None
                action = "upsert" if on_conflict else "insert"
                resp = await self._post_row("fan_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("fan_events", row_data)
                    action = "insert-fallback"
                self._log_result("fan_events", action, resp)
            except Exception:
                logger.exception("Failed to log fan event")

    async def log_privilege_advance(
        self,
        payload: dict,
        event,
        tiktok_username: str,
        raw_id: Optional[int] = None,
    ) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())

        def _get_child(obj, *names):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return _get_any(obj, *names)
            return _get_attr_any(obj, *names)

        def _is_missing(value) -> bool:
            if value is None:
                return True
            if isinstance(value, bool):
                return True
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return True
                return _is_placeholder_obj(text)
            if isinstance(value, (dict, list, tuple, set)):
                return len(value) == 0
            try:
                text = str(value).strip()
            except Exception:
                return True
            if not text:
                return True
            return _is_placeholder_obj(text)

        def _first_int(*values) -> Optional[int]:
            for value in values:
                if _is_missing(value):
                    continue
                coerced = self._to_int(value)
                if coerced is not None:
                    return coerced
            return None

        def _first_text(*values) -> Optional[str]:
            for value in values:
                if _is_missing(value):
                    continue
                normalized = _normalize_db_value(value)
                if _is_missing(normalized):
                    continue
                return str(normalized).strip()
            return None

        def _as_json_text(obj) -> Optional[str]:
            if obj is None:
                return None
            if isinstance(obj, (dict, list)):
                return _safe_json(obj)
            for method_name in ("to_dict", "as_dict"):
                try:
                    fn = getattr(obj, method_name, None)
                    if callable(fn):
                        converted = fn()
                        if isinstance(converted, (dict, list)):
                            return _safe_json(converted)
                except Exception:
                    pass
            try:
                parsed = json.loads(_safe_json(obj))
                if isinstance(parsed, (dict, list)):
                    return _safe_json(parsed)
            except Exception:
                pass
            return _normalize_db_value(obj)

        def _as_dict(obj) -> Optional[dict]:
            if obj is None:
                return None
            if isinstance(obj, dict):
                return obj
            for method_name in ("to_dict", "as_dict"):
                try:
                    fn = getattr(obj, method_name, None)
                    if callable(fn):
                        converted = fn()
                        if isinstance(converted, dict):
                            return converted
                except Exception:
                    pass
            try:
                converted = vars(obj)
                if isinstance(converted, dict):
                    return converted
            except Exception:
                pass
            return None

        def _pick_node(*values):
            for value in values:
                if _is_missing(value):
                    continue
                return value
            return None

        payload_map = payload if isinstance(payload, dict) else {}
        event_map = _as_dict(event) or {}

        base_message = _pick_node(
            _get_child(payload_map, "base_message", "baseMessage"),
            _get_child(event_map, "base_message", "baseMessage"),
            _get_attr_any(event, "base_message", "baseMessage"),
        )
        notify_obj = _pick_node(
            _get_child(payload_map, "notify"),
            _get_child(event_map, "notify"),
            _get_attr_any(event, "notify"),
        )
        scene_obj = _pick_node(
            _get_child(payload_map, "scene"),
            _get_child(event_map, "scene"),
            _get_attr_any(event, "scene"),
        )
        control_obj = _pick_node(
            _get_child(payload_map, "control"),
            _get_child(event_map, "control"),
            _get_attr_any(event, "control"),
        )
        left_icon_obj = _pick_node(
            _get_child(payload_map, "left_icon", "leftIcon"),
            _get_child(event_map, "left_icon", "leftIcon"),
            _get_attr_any(event, "left_icon", "leftIcon"),
        )
        right_icon_obj = _pick_node(
            _get_child(payload_map, "right_icon", "rightIcon"),
            _get_child(event_map, "right_icon", "rightIcon"),
            _get_attr_any(event, "right_icon", "rightIcon"),
        )
        background_obj = _pick_node(
            _get_child(payload_map, "background"),
            _get_child(event_map, "background"),
            _get_attr_any(event, "background"),
        )
        privilege_log_extra_obj = _pick_node(
            _get_child(payload_map, "privilege_log_extra", "privilegeLogExtra"),
            _get_child(event_map, "privilege_log_extra", "privilegeLogExtra"),
            _get_attr_any(event, "privilege_log_extra", "privilegeLogExtra"),
        )

        scene_text = _first_text(
            _get_attr_any(scene_obj, "name", "key"),
            scene_obj,
            _get_child(payload_map, "scene_name", "sceneName"),
            _get_child(event_map, "scene_name", "sceneName"),
            "UNKNOWN",
        )
        scene_value = _first_int(
            _get_attr_any(scene_obj, "value", "number", "id"),
            _get_child(payload_map, "scene_value", "sceneValue"),
            _get_child(event_map, "scene_value", "sceneValue"),
            scene_obj,
            0,
        )
        control_target_group = _pick_node(
            _get_child(control_obj, "target_group_show_rst", "targetGroupShowRst"),
        )
        control_status_from_groups = None
        if isinstance(control_target_group, dict) and control_target_group:
            total_groups = len(control_target_group)
            banned_groups = 0
            for result in control_target_group.values():
                banned = _get_child(result, "banned")
                if banned is True or str(banned).strip().lower() in {"1", "true", "yes"}:
                    banned_groups += 1
            control_status_from_groups = f"banned={banned_groups}/{total_groups}"
        control_trigger_type = _pick_node(
            _get_child(control_obj, "horizontal_trigger_type", "horizontalTriggerType"),
        )
        notify_extra = _pick_node(
            _get_child(notify_obj, "extra"),
        )
        notify_content = _first_text(
            _get_child(notify_obj, "content", "notify_content", "notifyContent", "text", "title"),
            _get_child(notify_extra, "content", "text", "title", "description"),
            _get_child(payload_map, "content", "notify_content", "notifyContent"),
            _get_child(base_message, "describe"),
            _get_child(_get_child(base_message, "display_text", "displayText"), "default_pattern", "defaultPattern"),
        )

        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "event_type": "privilege_advance",
            "method": _first_text(
                _get_child(payload_map, "method"),
                _get_child(event_map, "method"),
                _get_child(base_message, "method"),
                _get_attr_any(event, "method"),
                "WebcastPrivilegeAdvanceMessage",
            ),
            "room_id": _first_int(
                _get_child(payload_map, "room_id", "roomId"),
                _get_child(event_map, "room_id", "roomId"),
                _get_child(base_message, "room_id", "roomId"),
                _get_attr_any(event, "room_id", "roomId"),
            ),
            "create_time_ms": _first_int(
                _get_child(payload_map, "create_time", "create_time_ms", "createTime", "timestamp"),
                _get_child(event_map, "create_time", "create_time_ms", "createTime", "timestamp"),
                _get_child(base_message, "create_time", "createTime"),
                _get_attr_any(event, "create_time", "createTime", "timestamp"),
                int(unix_ts * 1000),
            ),
            "message_id": _first_int(
                _get_child(payload_map, "msg_id", "msgId", "message_id", "messageId", "event_id"),
                _get_child(event_map, "msg_id", "msgId", "message_id", "messageId", "event_id"),
                _get_child(base_message, "message_id", "messageId"),
                _get_attr_any(event, "msg_id", "msgId", "message_id", "messageId", "_ws_msg_id"),
            ),
            "sub_type": _first_text(
                _get_child(payload_map, "sub_type", "subType"),
                _get_child(event_map, "sub_type", "subType"),
                _get_attr_any(event, "sub_type", "subType"),
            ),
            "scene": scene_text,
            "scene_value": scene_value,
            "control_display_type": _first_int(
                _get_child(control_obj, "display_type", "displayType", "type"),
                _get_attr_any(control_trigger_type, "value", "number", "id"),
                _get_child(control_obj, "horizontal_trigger_type_value", "horizontalTriggerTypeValue"),
                _get_child(control_obj, "priority"),
            ),
            "control_display_status": _first_text(
                _get_child(control_obj, "display_status", "displayStatus", "status"),
                control_trigger_type,
                control_status_from_groups,
                _get_child(control_obj, "duration"),
            ),
            "left_icon_uri": _first_text(
                _get_child(left_icon_obj, "m_uri", "mUri", "uri"),
            ),
            "right_icon_uri": _first_text(
                _get_child(right_icon_obj, "m_uri", "mUri", "uri"),
            ),
            "background_uri": _first_text(
                _get_child(background_obj, "m_uri", "mUri", "uri"),
            ),
            "notify_type": _first_int(
                _get_child(notify_obj, "notify_type", "notifyType", "type"),
            ),
            "notify_content": notify_content,
            "notify_schema": _first_text(
                _get_child(notify_obj, "schema", "schema_url", "schemaUrl"),
            ),
            "privilege_id": _first_text(
                _get_child(privilege_log_extra_obj, "privilege_id", "privilegeId"),
            ),
            "privilege_version": _first_text(
                _get_child(privilege_log_extra_obj, "privilege_version", "privilegeVersion"),
            ),
            "privilege_level": _first_text(
                _get_child(privilege_log_extra_obj, "level", "privilege_level", "privilegeLevel"),
            ),
            "privilege_data_version": _first_text(
                _get_child(privilege_log_extra_obj, "data_version", "dataVersion"),
            ),
            "privilege_order_id": _first_text(
                _get_child(privilege_log_extra_obj, "privilege_order_id", "privilegeOrderId"),
            ),
            "notify_json": _as_json_text(notify_obj),
            "scene_json": _as_json_text(scene_obj),
            "control_json": _as_json_text(control_obj),
            "left_icon_json": _as_json_text(left_icon_obj),
            "right_icon_json": _as_json_text(right_icon_obj),
            "background_json": _as_json_text(background_obj),
            "privilege_log_extra_json": _as_json_text(privilege_log_extra_obj),
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping privilege_advance_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                for key in (
                    "room_id",
                    "create_time_ms",
                    "message_id",
                    "scene_value",
                    "control_display_type",
                    "notify_type",
                ):
                    row_data[key] = self._to_int(row_data.get(key))
                on_conflict = "message_id,tiktok_username" if row_data.get("message_id") else None
                action = "upsert" if on_conflict else "insert"
                resp = await self._post_row("privilege_advance_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("privilege_advance_events", row_data)
                    action = "insert-fallback"
                self._log_result("privilege_advance_events", action, resp)
            except Exception:
                logger.exception("Failed to log privilege advance event")

    async def log_listener_heartbeat(self, listener_id: str, payload: dict) -> None:
        listener_id = str(listener_id or "").strip()
        if not listener_id:
            return
        now_dt = datetime.now(timezone.utc)
        updated_unix = int(now_dt.timestamp())

        listener_usernames_raw = payload.get("listener_usernames") if isinstance(payload, dict) else None
        if not isinstance(listener_usernames_raw, list):
            listener_usernames_raw = []
        listener_usernames = [str(item).strip() for item in listener_usernames_raw if str(item).strip()]

        recent_logs_raw = payload.get("recent_logs") if isinstance(payload, dict) else None
        if not isinstance(recent_logs_raw, list):
            recent_logs_raw = []
        recent_logs = [str(item)[:1000] for item in recent_logs_raw if str(item).strip()]
        recent_logs = recent_logs[-100:]

        listener_states = payload.get("listener_states") if isinstance(payload, dict) else None
        if not isinstance(listener_states, list):
            listener_states = []

        row = {
            "listener_id": listener_id,
            "updated_at": now_dt.isoformat(),
            "updated_unix": updated_unix,
            "status": _normalize_db_value(payload.get("status")) if isinstance(payload, dict) else "running",
            "process_id": self._to_int(payload.get("process_id")) if isinstance(payload, dict) else None,
            "hostname": _normalize_db_value(payload.get("hostname")) if isinstance(payload, dict) else None,
            "machine_id": _normalize_db_value(payload.get("machine_id")) if isinstance(payload, dict) else None,
            "region": _normalize_db_value(payload.get("region")) if isinstance(payload, dict) else None,
            "active_listener_count": self._to_int(payload.get("active_listener_count")) if isinstance(payload, dict) else None,
            "connected_listener_count": self._to_int(payload.get("connected_listener_count")) if isinstance(payload, dict) else None,
            "queue_total": self._to_int(payload.get("queue_total")) if isinstance(payload, dict) else None,
            "backpressure_hits_total": self._to_int(payload.get("backpressure_hits_total")) if isinstance(payload, dict) else None,
            "last_log_line": _normalize_db_value(payload.get("last_log_line")) if isinstance(payload, dict) else None,
            "listener_usernames": listener_usernames,
            "listener_states": listener_states,
            "recent_logs": recent_logs,
            "meta": payload if isinstance(payload, dict) else {},
        }
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping listener heartbeat insert")
                    return
                resp = await self._send_post_with_retry(
                    table="listener_heartbeats",
                    row_data=row,
                    on_conflict="listener_id",
                    prefer=None,
                    queue_on_failure=False,
                    max_attempts=1,
                )
                self._log_result("listener_heartbeats", "upsert", resp)
            except Exception:
                logger.exception("Failed to write listener heartbeat")

    async def close(self) -> None:
        # Best-effort drain any pending batched writes before closing client.
        pending_tasks = []
        async with self._batch_lock:
            for state in self._batch_states.values():
                task = state.get("task")
                if task and not task.done():
                    pending_tasks.append(task)
        if pending_tasks:
            try:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            except Exception:
                pass

        if not self._client:
            return
        try:
            await self._client.aclose()
        except Exception:
            pass
        self._client = None

    async def log_event(
        self,
        event_type: str,
        payload: dict,
        tiktok_username: str,
        raw_id: Optional[int] = None,
    ) -> Optional[int]:
        if not self._write_tiktok_events_raw:
            if self._debug:
                logger.debug("Skipping tiktok_events_raw insert (disabled): event_type=%s", event_type)
            return None
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())
        event_id = raw_id if raw_id is not None else self._next_raw_id()
        payload_json = _safe_json(payload)
        payload_value = None
        msg_id_value = None
        try:
            parsed = json.loads(payload_json)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            payload_value = parsed.get("payload")
            msg_id_value = _get_any(parsed, "msgId", "msg_id", "messageId", "message_id")
            if msg_id_value is None:
                base_message = parsed.get("baseMessage") or parsed.get("base_message")
                if isinstance(base_message, dict):
                    msg_id_value = _get_any(base_message, "msgId", "msg_id", "messageId", "message_id")
        if isinstance(msg_id_value, str):
            try:
                msg_id_value = int(msg_id_value)
            except Exception:
                pass
        if isinstance(payload_value, (bytes, bytearray)):
            try:
                payload_value = base64.b64encode(bytes(payload_value)).decode("ascii")
            except Exception:
                payload_value = None
        elif payload_value is not None and not isinstance(payload_value, str):
            payload_value = str(payload_value)
        row = {
            "id": event_id,
            "event_type": event_type,
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "payload": payload_value,
            "msgId": msg_id_value,
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping tiktok_events_raw insert")
                    return None
                row_data = dict(zip(row.keys(), values))
                resp = await self._post_row("tiktok_events_raw", row_data)
                self._log_result("tiktok_events_raw", "insert", resp)
                if resp is None or not resp.is_success:
                    return None
                return event_id
            except Exception:
                logger.exception("Failed to log tiktok_events_raw")
                return None

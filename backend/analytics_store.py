from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import httpx

logger = logging.getLogger("analytics-store")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    data = path.read_text(encoding="utf-8")
    for raw_line in data.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and ((value[0] == value[-1]) and value[0] in {"'", '"'}):
            value = value[1:-1]
        env.setdefault(key, value)
    return env


def _load_env_from_config() -> dict[str, str]:
    repo_root = Path(__file__).resolve().parent.parent
    for name in ("tiktok_listener.env", ".env.tiktok", ".env"):
        candidate = repo_root / name
        if candidate.exists() and candidate.is_file():
            return _load_env_file(candidate)
    return {}


ENV = _load_env_from_config()


def _env(key: str, default: str = "") -> str:
    if key in ENV:
        return ENV.get(key, default)
    return os.environ.get(key, default)


class AnalyticsStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._client: Optional[httpx.Client] = None
        self._rest_url = ""
        self._enabled = False
        self._row_id_seq = int(time.time() * 1000) % 1000
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=2000)
        self._worker: Optional[threading.Thread] = None
        self._init_client()

    def _init_client(self) -> None:
        project_id = _env("SUPABASE_PROJECT_ID", "").strip()
        if project_id:
            url = f"https://{project_id}.supabase.co"
        key = _env("SUPABASE_SECRET_KEY").strip()
        if not url or not key:
            logger.warning(
                "Supabase credentials missing. Set SUPABASE_URL or SUPABASE_PROJECT_ID and SUPABASE_SECRET_KEY."
            )
            return
        self._rest_url = url.rstrip("/") + "/rest/v1"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(base_url=self._rest_url, headers=headers, timeout=30.0)
        self._enabled = True
        logger.info("Supabase analytics client initialized for %s", self._rest_url)
        self._start_worker()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _next_row_id(self) -> int:
        # millisecond timestamp * 1000 + sequence to avoid collisions within the same ms
        base = int(time.time() * 1000) * 1000
        self._row_id_seq = (self._row_id_seq + 1) % 1000
        return base + self._row_id_seq

    def _start_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._worker_loop, name="analytics-worker", daemon=True)
        self._worker.start()

    def _enqueue(self, task: tuple) -> None:
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            logger.warning("Analytics queue full; dropping task %s", task[0] if task else "unknown")

    def _worker_loop(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if not task:
                    continue
                kind = task[0]
                if kind == "play":
                    row = task[1]
                    resp = self._post_row("play_events", row)
                    self._log_result("play_events", "insert", resp)
            except Exception as exc:
                logger.exception("Analytics worker error: %s", exc)
            finally:
                self._queue.task_done()

    def _post_row(
        self,
        table: str,
        row_data: dict,
        on_conflict: Optional[str] = None,
        prefer: Optional[str] = None,
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
        return self._client.post(
            f"/{table}",
            params=params,
            json=row_data,
            headers={"Prefer": prefer_header},
        )

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

    def _get_rows(self, table: str, params: dict) -> Optional[list]:
        if not self._client:
            return None
        try:
            resp = self._client.get(f"/{table}", params=params)
        except Exception as exc:
            logger.warning("Supabase GET %s failed: %s", table, exc)
            return None
        if not resp.is_success:
            logger.warning("Supabase GET %s failed (status=%s): %s", table, resp.status_code, resp.text)
            return None
        try:
            payload = resp.json()
        except Exception:
            return None
        return payload if isinstance(payload, list) else None

    def _patch_rows(self, table: str, filters: dict, row_data: dict) -> Optional[httpx.Response]:
        if not self._client:
            return None
        return self._client.patch(
            f"/{table}",
            params=filters,
            json=row_data,
            headers={"Prefer": "return=minimal"},
        )

    def _delete_rows(self, table: str, filters: dict) -> Optional[httpx.Response]:
        if not self._client:
            return None
        return self._client.delete(
            f"/{table}",
            params=filters,
            headers={"Prefer": "return=minimal"},
        )

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

    def migrate_from_json(
        self,
        plays_path: Optional[Path],
        points_path: Optional[Path],
        cleanup: bool = False,
    ) -> bool:
        # song_stats removed; keep signature for compatibility
        if cleanup:
            for path in (plays_path, points_path):
                try:
                    if path and path.exists():
                        path.unlink()
                except Exception:
                    pass
        return False

    def get_play_events(self, limit: int = 200, offset: int = 0) -> list[dict]:
        if not self._enabled:
            return []
        rows = self._get_rows(
            "play_events",
            {
                "select": "id,song_url,song_name,unix_ts,iso_ts,date,time,assigned_dancers,slot_one,slot_two,"
                "score_slot_one,score_slot_two,battle_active,battle_mode,group_name,context",
                "order": "id.desc",
                "limit": limit,
                "offset": offset,
            },
        )
        return rows or []

    def get_play_events_csv(self, limit: int = 1000, offset: int = 0) -> list[list]:
        if not self._enabled:
            return []
        header = [
            "id",
            "song_url",
            "song_name",
            "unix_ts",
            "iso_ts",
            "date",
            "time",
            "assigned_dancers",
            "slot_one",
            "slot_two",
            "score_slot_one",
            "score_slot_two",
            "battle_active",
            "battle_mode",
            "group_name",
            "context",
        ]
        rows = [header]
        for row in self.get_play_events(limit=limit, offset=offset):
            rows.append([row.get(h) for h in header])
        return rows

    def load_stats(self) -> Dict[str, Dict[str, int]]:
        if not self._enabled:
            return {}
        out: Dict[str, Dict[str, int]] = {}
        # Aggregate play counts + points from play_events (diamond_count)
        limit = 2000
        offset = 0
        while True:
            rows = self._get_rows(
                "play_events",
                {"select": "song_url,diamond_count", "limit": limit, "offset": offset},
            )
            if not rows:
                break
            for row in rows:
                url = (row.get("song_url") or "").strip()
                if not url:
                    continue
                entry = out.setdefault(url, {"play_count": 0, "points_count": 0})
                entry["play_count"] = int(entry["play_count"]) + 1
                entry["points_count"] = int(entry["points_count"]) + int(row.get("diamond_count") or 0)
            if len(rows) < limit:
                break
            offset += limit
        return out

    def increment_play(
        self,
        song_url: str,
        song_name: str,
        unix_ts: int,
        iso_ts: str,
        date: str,
        time_of_day: str,
        assigned_dancers: str,
        slot_one: str,
        slot_two: str,
        score_one: int,
        score_two: int,
        battle_active: bool,
        battle_mode: str,
        group_name: str,
        context: str,
        duration_sec: Optional[int],
        play_count: Optional[int] = None,
        points_count: Optional[int] = None,
    ) -> None:
        if not self._enabled:
            return
        row = {
            "id": self._next_row_id(),
            "song_url": song_url,
            "song_name": song_name,
            "unix_ts": unix_ts,
            "iso_ts": iso_ts,
            "date": date,
            "time": time_of_day,
            "assigned_dancers": assigned_dancers,
            "slot_one": slot_one,
            "slot_two": slot_two,
            "score_slot_one": score_one,
            "score_slot_two": score_two,
            "battle_active": 1 if battle_active else 0,
            "battle_mode": battle_mode,
            "group_name": group_name,
            "context": context,
            "duration": duration_sec,
        }
        self._enqueue(("play", row))

    def increment_points(
        self,
        song_url: str,
        amount: int,
        unix_ts: int,
        iso_ts: str,
        play_count: Optional[int] = None,
        points_count: Optional[int] = None,
    ) -> None:
        # point_events table removed; keep for API compatibility
        return

    def remap_song_url(self, old_url: str, new_url: str) -> None:
        if not old_url or not new_url or old_url == new_url:
            return
        if not self._enabled:
            return
        self._patch_rows("play_events", {"song_url": f"eq.{old_url}"}, {"song_url": new_url})
        # song_stats removed; counts derived from events

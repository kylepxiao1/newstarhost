"""Persistence for one group's data (song library + small documents).

SupabaseGroupStore is used by the live app: reads happen once when the group is first
loaded; writes go through a background thread (only changed songs are sent) so requests
never wait on the database. LocalGroupStore keeps the old media/*.json layout and is used
only by the offline bundle.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from backend.supabase_rest import SupabaseRest, chunked

logger = logging.getLogger("group-store")

# Offline file names for each document key (kept compatible with the old media/ layout).
LOCAL_KV_FILES = {
    "dancers": "dancers.json",
    "settings": "hotkeys.json",
    "role_presets": "role_presets.json",
    "rpd_queries": "rpd_queries.json",
    "gift_notes": "gift_notes.json",
    "play_counts": "play_counts.json",
    "points_counts": "points_counts.json",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class GroupStore:
    def load_library(self) -> dict:
        raise NotImplementedError

    def save_library(self, library: dict) -> None:
        raise NotImplementedError

    def load_kv(self, key: str, default: Any) -> Any:
        raise NotImplementedError

    def save_kv(self, key: str, value: Any) -> None:
        raise NotImplementedError

    def flush(self, timeout: float = 30.0) -> None:
        pass


class SupabaseGroupStore(GroupStore):
    _RETRIES = 5

    def __init__(self, rest: SupabaseRest, group_id: str) -> None:
        self.rest = rest
        self.group_id = group_id
        self._saved_songs: dict[str, str] = {}  # song_id -> fingerprint last sent
        self._lock = threading.Lock()
        self._queue: "queue.Queue[Optional[tuple[str, Callable[[], None]]]]" = queue.Queue()
        self._worker = threading.Thread(target=self._run, name=f"group-store-{group_id[:8]}", daemon=True)
        self._worker.start()

    # --- reads (only at group load) ---
    def load_library(self) -> dict:
        rows = self.rest.select("group_songs", {"select": "song_id,data", "group_id": f"eq.{self.group_id}"})
        library = {r["song_id"]: r["data"] for r in rows if isinstance(r.get("data"), dict)}
        with self._lock:
            self._saved_songs = {sid: _fingerprint(meta) for sid, meta in library.items()}
        return library

    def load_kv(self, key: str, default: Any) -> Any:
        row = self.rest.select_one("group_kv", {"select": "value", "group_id": f"eq.{self.group_id}", "key": f"eq.{key}"})
        return row["value"] if row else default

    # --- writes (background) ---
    def save_library(self, library: dict) -> None:
        current = {sid: _fingerprint(meta) for sid, meta in library.items()}
        with self._lock:
            changed = [sid for sid, fp in current.items() if self._saved_songs.get(sid) != fp]
            removed = [sid for sid in self._saved_songs if sid not in current]
            self._saved_songs = current
        if changed:
            rows = [
                {"group_id": self.group_id, "song_id": sid, "data": json.loads(current[sid]), "updated_at": _now()}
                for sid in changed
            ]
            for batch in chunked(rows, 200):
                self._enqueue(
                    f"upsert {len(batch)} song(s)",
                    lambda b=batch: self.rest.upsert("group_songs", b, on_conflict="group_id,song_id"),
                )
        for batch in chunked(removed, 100):
            ids = ",".join(f'"{sid}"' for sid in batch)
            self._enqueue(
                f"delete {len(batch)} song(s)",
                lambda ids=ids: self.rest.delete("group_songs", {"group_id": f"eq.{self.group_id}", "song_id": f"in.({ids})"}),
            )

    def save_kv(self, key: str, value: Any) -> None:
        row = {"group_id": self.group_id, "key": key, "value": json.loads(_fingerprint(value)), "updated_at": _now()}
        self._enqueue(f"save {key}", lambda: self.rest.upsert("group_kv", row, on_conflict="group_id,key"))

    def _enqueue(self, label: str, op: Callable[[], None]) -> None:
        self._queue.put((label, op))

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                label, op = item
                for attempt in range(self._RETRIES):
                    try:
                        op()
                        break
                    except Exception as exc:
                        if attempt == self._RETRIES - 1:
                            logger.error("Group %s: %s failed permanently: %s", self.group_id, label, exc)
                        else:
                            time.sleep(min(10, 2 ** attempt))
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)


class LocalGroupStore(GroupStore):
    """media/*.json files (offline bundle only)."""

    def __init__(self, media_dir: Path) -> None:
        self.media_dir = Path(media_dir)

    def _read(self, name: str, default: Any) -> Any:
        path = self.media_dir / name
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
        except Exception:
            return default

    def _write(self, name: str, value: Any) -> None:
        self.media_dir.mkdir(parents=True, exist_ok=True)
        path = self.media_dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def load_library(self) -> dict:
        data = self._read("songs.json", {})
        return data if isinstance(data, dict) else {}

    def save_library(self, library: dict) -> None:
        self._write("songs.json", library)

    def load_kv(self, key: str, default: Any) -> Any:
        return self._read(LOCAL_KV_FILES.get(key, f"{key}.json"), default)

    def save_kv(self, key: str, value: Any) -> None:
        self._write(LOCAL_KV_FILES.get(key, f"{key}.json"), value)

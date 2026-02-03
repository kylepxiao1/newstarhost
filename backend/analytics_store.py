from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, Optional


class AnalyticsStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS song_stats (
                song_url TEXT PRIMARY KEY,
                play_count INTEGER NOT NULL DEFAULT 0,
                points_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS play_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                song_url TEXT NOT NULL,
                song_name TEXT,
                unix_ts INTEGER,
                iso_ts TEXT,
                date TEXT,
                time TEXT,
                assigned_dancers TEXT,
                slot_one TEXT,
                slot_two TEXT,
                score_slot_one INTEGER,
                score_slot_two INTEGER,
                battle_active INTEGER,
                battle_mode TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS point_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                song_url TEXT NOT NULL,
                amount INTEGER NOT NULL,
                unix_ts INTEGER,
                iso_ts TEXT
            )
            """
        )
        self._conn.commit()

    def migrate_from_json(self, plays_path: Optional[Path], points_path: Optional[Path], cleanup: bool = False) -> bool:
        if not plays_path and not points_path:
            return False
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM song_stats")
        row = cur.fetchone()
        if row and row["cnt"]:
            return False
        stats: Dict[str, Dict[str, int]] = {}
        if plays_path and plays_path.exists():
            try:
                data = json.loads(plays_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k, v in data.items():
                        stats.setdefault(str(k), {})["play_count"] = int(v)
            except Exception:
                pass
        if points_path and points_path.exists():
            try:
                data = json.loads(points_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k, v in data.items():
                        stats.setdefault(str(k), {})["points_count"] = int(v)
            except Exception:
                pass
        if not stats:
            return False
        for url, vals in stats.items():
            cur.execute(
                "INSERT OR REPLACE INTO song_stats (song_url, play_count, points_count) VALUES (?, ?, ?)",
                (url, vals.get("play_count", 0), vals.get("points_count", 0)),
            )
        self._conn.commit()
        if cleanup:
            for path in (plays_path, points_path):
                try:
                    if path and path.exists():
                        path.unlink()
                except Exception:
                    pass
        return True

    def get_play_events(self, limit: int = 200, offset: int = 0) -> list[dict]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id, song_url, song_name, unix_ts, iso_ts, date, time, assigned_dancers, "
            "slot_one, slot_two, score_slot_one, score_slot_two, battle_active, battle_mode "
            "FROM play_events ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        out = []
        for row in cur.fetchall():
            out.append({k: row[k] for k in row.keys()})
        return out

    def get_play_events_csv(self, limit: int = 1000, offset: int = 0) -> list[list]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id, song_url, song_name, unix_ts, iso_ts, date, time, assigned_dancers, "
            "slot_one, slot_two, score_slot_one, score_slot_two, battle_active, battle_mode "
            "FROM play_events ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        header = [d[0] for d in cur.description]
        rows = [header]
        for row in cur.fetchall():
            rows.append([row[h] for h in header])
        return rows

    def load_stats(self) -> Dict[str, Dict[str, int]]:
        cur = self._conn.cursor()
        cur.execute("SELECT song_url, play_count, points_count FROM song_stats")
        out: Dict[str, Dict[str, int]] = {}
        for row in cur.fetchall():
            out[row["song_url"]] = {
                "play_count": int(row["play_count"] or 0),
                "points_count": int(row["points_count"] or 0),
            }
        return out

    def increment_play(self, song_url: str, song_name: str, unix_ts: int, iso_ts: str, date: str, time_of_day: str,
                       assigned_dancers: str, slot_one: str, slot_two: str, score_one: int, score_two: int,
                       battle_active: bool, battle_mode: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO play_events (song_url, song_name, unix_ts, iso_ts, date, time, assigned_dancers, slot_one, slot_two, score_slot_one, score_slot_two, battle_active, battle_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                song_url,
                song_name,
                unix_ts,
                iso_ts,
                date,
                time_of_day,
                assigned_dancers,
                slot_one,
                slot_two,
                score_one,
                score_two,
                1 if battle_active else 0,
                battle_mode,
            ),
        )
        cur.execute(
            "INSERT INTO song_stats (song_url, play_count, points_count) VALUES (?, 1, 0) "
            "ON CONFLICT(song_url) DO UPDATE SET play_count = play_count + 1",
            (song_url,),
        )
        self._conn.commit()

    def increment_points(self, song_url: str, amount: int, unix_ts: int, iso_ts: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO point_events (song_url, amount, unix_ts, iso_ts) VALUES (?, ?, ?, ?)",
            (song_url, amount, unix_ts, iso_ts),
        )
        cur.execute(
            "INSERT INTO song_stats (song_url, play_count, points_count) VALUES (?, 0, ?) "
            "ON CONFLICT(song_url) DO UPDATE SET points_count = points_count + ?",
            (song_url, amount, amount),
        )
        self._conn.commit()

    def remap_song_url(self, old_url: str, new_url: str) -> None:
        if not old_url or not new_url or old_url == new_url:
            return
        cur = self._conn.cursor()
        cur.execute("UPDATE play_events SET song_url=? WHERE song_url=?", (new_url, old_url))
        cur.execute("UPDATE point_events SET song_url=? WHERE song_url=?", (new_url, old_url))
        cur.execute(
            "INSERT INTO song_stats (song_url, play_count, points_count) "
            "SELECT ?, play_count, points_count FROM song_stats WHERE song_url=? "
            "ON CONFLICT(song_url) DO UPDATE SET play_count=play_count+excluded.play_count, points_count=points_count+excluded.points_count",
            (new_url, old_url),
        )
        cur.execute("DELETE FROM song_stats WHERE song_url=?", (old_url,))
        self._conn.commit()

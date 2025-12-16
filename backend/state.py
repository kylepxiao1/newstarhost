from __future__ import annotations

import threading
import time
import json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, Any, List


@dataclass
class BattleState:
    active: bool = False
    battle_mode: str = "standard"
    slot_one: Optional[str] = None
    slot_two: Optional[str] = None
    scores: Dict[str, int] = field(default_factory=lambda: {"slot_one": 0, "slot_two": 0})
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    overlay_states: Dict[str, bool] = field(default_factory=dict)
    current_scene: str = "MainScene"
    camera_index: int = -1
    camera_label: str = ""
    last_winner: str = ""
    group_name: str = ""
    enabled_dancers: List[str] = field(default_factory=list)
    win_counts: Dict[str, int] = field(default_factory=lambda: {"slot_one": 0, "slot_two": 0})
    songs: Dict[str, str] = field(
        default_factory=lambda: {
            "slot_one": "",
            "slot_two": "",
            "group": "",
            "background": "",
            "current": "",
            "position": "0",
            "library": {},
        }
    )
    dancers: List[Dict[str, str]] = field(default_factory=list)
    play_counts: Dict[str, int] = field(default_factory=dict)
    points_counts: Dict[str, int] = field(default_factory=dict)

    def copy(self) -> Dict:
        return asdict(self)


class BattleStateManager:
    def __init__(self, overlay_names, library_path: Optional[Path] = None, dancers_path: Optional[Path] = None, plays_path: Optional[Path] = None, points_path: Optional[Path] = None) -> None:
        self._library_path: Optional[Path] = Path(library_path) if library_path else None
        self._dancers_path: Optional[Path] = Path(dancers_path) if dancers_path else None
        self._plays_path: Optional[Path] = Path(plays_path) if plays_path else None
        self._points_path: Optional[Path] = Path(points_path) if points_path else None
        library = self._load_library()
        dancers = self._load_dancers()
        plays = self._load_play_counts()
        points = self._load_points_counts()
        self._state = BattleState(overlay_states={name: (name != "BurstOverlay") for name in overlay_names})
        self._state.songs["library"] = library
        self._state.dancers = dancers
        self._state.play_counts = plays
        self._state.points_counts = points
        if not self._state.enabled_dancers:
            enabled = []
            default_group = ""
            for d in dancers:
                name = (d.get("name") or "").strip()
                if not name:
                    continue
                low = name.lower()
                if not default_group and ("boys" in low or "girls" in low):
                    default_group = name
                if "boys" in low or "girls" in low:
                    continue
                enabled.append(name)
            self._state.enabled_dancers = enabled
            if default_group:
                self._state.group_name = default_group
        self._lock = threading.RLock()

    def get_state(self) -> Dict:
        with self._lock:
            return self._state.copy()

    def start_battle(self, mode: Optional[str] = None) -> Dict:
        with self._lock:
            self._state.active = True
            if mode:
                self._state.battle_mode = mode
            self._state.start_time = time.time()
            self._state.end_time = None
            self._state.scores = {"slot_one": 0, "slot_two": 0}
            return self._state.copy()

    def end_battle(self) -> Dict:
        with self._lock:
            self._state.active = False
            self._state.end_time = time.time()
            return self._state.copy()

    def assign_slot(self, slot: str, name: Optional[str]) -> Dict:
        if slot not in ("slot_one", "slot_two"):
            raise ValueError("Slot must be 'slot_one' or 'slot_two'")
        with self._lock:
            setattr(self._state, slot, name)
            return self._state.copy()

    def import_slots(self, slot_one: Optional[str], slot_two: Optional[str]) -> Dict:
        with self._lock:
            self._state.slot_one = slot_one
            self._state.slot_two = slot_two
            return self._state.copy()

    def increment_score(self, slot: str, amount: int = 1) -> Dict:
        if slot not in ("slot_one", "slot_two"):
            raise ValueError("Slot must be 'slot_one' or 'slot_two'")
        with self._lock:
            self._state.scores[slot] = self._state.scores.get(slot, 0) + amount
            return self._state.copy()

    def set_overlay_state(self, name: str, visible: bool) -> Dict:
        with self._lock:
            self._state.overlay_states[name] = visible
            return self._state.copy()

    def set_scene(self, scene: str) -> Dict:
        with self._lock:
            self._state.current_scene = scene
            return self._state.copy()

    def set_camera_index(self, idx: int) -> Dict:
        with self._lock:
            self._state.camera_index = idx
            return self._state.copy()

    def set_camera_label(self, label: str) -> Dict:
        with self._lock:
            self._state.camera_label = label
            return self._state.copy()

    def set_song(self, target: str, url: str) -> Dict:
        with self._lock:
            self._state.songs[target] = url
            return self._state.copy()

    def set_current_song(self, target: str, url: str) -> Dict:
        with self._lock:
            self._state.songs["current"] = url
            # also store last for target if provided
            if target:
                self._state.songs[target] = url
            return self._state.copy()

    def set_song_position(self, pos: float) -> Dict:
        with self._lock:
            self._state.songs["position"] = str(pos)
            return self._state.copy()

    def register_song(self, song_id: str, name: str, url: str, dancers: Optional[list] = None, front_dancers: Optional[list] = None, mvp_dancers: Optional[list] = None, roles: Optional[list] = None, knows_song: Optional[list] = None, exclusive_mvp_for: Optional[str] = None) -> Dict:
        with self._lock:
            lib = self._state.songs.get("library", {})
            roles_list = roles or []
            if roles_list:
                lib = self._clear_roles(roles_list, song_id, lib)
            lib[song_id] = {
                "name": name,
                "url": url,
                "dancers": dancers or [],
                "front_dancers": front_dancers or [],
                "mvp_dancers": mvp_dancers or [],
                "roles": roles_list,
                "knows_song": knows_song or [],
                "exclusive_mvp_for": exclusive_mvp_for or "",
            }
            self._state.songs["library"] = lib
            self._persist_library(lib)
            return self._state.copy()

    def tag_song(self, url: str, dancer: str) -> Dict:
        with self._lock:
            lib = self._state.songs.get("library", {})
            for key, val in lib.items():
                if val.get("url") == url:
                    dancers = set(val.get("dancers", []))
                    dancers.add(dancer)
                    val["dancers"] = list(dancers)
                    lib[key] = val
            self._state.songs["library"] = lib
            self._persist_library(lib)
            return self._state.copy()

    def update_song_dancers(self, song_id: str, dancers: list, front_dancers: list, mvp_dancers: list, roles: Optional[list] = None, knows_song: Optional[list] = None, exclusive_mvp_for: Optional[str] = None) -> Dict:
        with self._lock:
            lib = self._state.songs.get("library", {})
            if song_id in lib:
                if roles is not None:
                    lib = self._clear_roles(roles, song_id, lib)
                lib[song_id]["dancers"] = dancers
                lib[song_id]["front_dancers"] = front_dancers
                lib[song_id]["mvp_dancers"] = mvp_dancers
                if roles is not None:
                    lib[song_id]["roles"] = roles
                else:
                    lib[song_id].setdefault("roles", [])
                if knows_song is not None:
                    lib[song_id]["knows_song"] = knows_song
                else:
                    lib[song_id].setdefault("knows_song", [])
                if exclusive_mvp_for is not None:
                    lib[song_id]["exclusive_mvp_for"] = exclusive_mvp_for
                else:
                    lib[song_id].setdefault("exclusive_mvp_for", "")
                self._state.songs["library"] = lib
                self._persist_library(lib)
            return self._state.copy()

    def rename_song(self, song_id: str, name: str) -> Dict:
        with self._lock:
            lib = self._state.songs.get("library", {})
            if song_id in lib:
                lib[song_id]["name"] = name
                self._state.songs["library"] = lib
                self._persist_library(lib)
            return self._state.copy()

    def delete_song(self, song_id: str) -> Dict:
        with self._lock:
            lib = self._state.songs.get("library", {})
            if song_id in lib:
                lib.pop(song_id, None)
                self._state.songs["library"] = lib
                self._persist_library(lib)
            return self._state.copy()

    def add_dancer(self, name: str, handle: str) -> Dict:
        with self._lock:
            dancers = self._state.dancers or []
            name_l = (name or "").lower()
            handle_l = (handle or "").lower()
            replaced = False
            for idx, d in enumerate(dancers):
                if d.get("name", "").lower() == name_l or d.get("handle", "").lower() == handle_l:
                    dancers[idx] = {"name": name, "handle": handle}
                    replaced = True
                    break
            if not replaced:
                dancers.append({"name": name, "handle": handle})
            self._state.dancers = dancers
            self._persist_dancers(dancers)
            return self._state.copy()

    def delete_dancer(self, name: str) -> Dict:
        with self._lock:
            target = (name or "").lower()
            dancers = [d for d in (self._state.dancers or []) if (d.get("name","").lower() != target and d.get("handle","").lower() != target)]
            self._state.dancers = dancers
            self._persist_dancers(dancers)
            # remove from enabled
            self._state.enabled_dancers = [d for d in (self._state.enabled_dancers or []) if d.lower() != target]
            # clear last_winner/group if matches
            if (self._state.last_winner or "").lower() == target:
                self._state.last_winner = ""
            if (self._state.group_name or "").lower() == target:
                self._state.group_name = ""
            # drop win counts
            wins = self._state.win_counts or {}
            wins.pop(name, None)
            self._state.win_counts = wins
            # remove from songs
            lib = self._state.songs.get("library", {})
            changed = False
            for sid, val in lib.items():
                for key in ["dancers", "front_dancers", "mvp_dancers", "knows_song"]:
                    if key in val and isinstance(val[key], list):
                        before = len(val[key])
                        val[key] = [x for x in val[key] if (x or "").lower() != target]
                        if len(val[key]) != before:
                            changed = True
            if changed:
                self._persist_library(lib)
            return self._state.copy()

    def set_last_winner(self, name: str) -> Dict:
        with self._lock:
            self._state.last_winner = name
            return self._state.copy()

    def set_group_name(self, name: str) -> Dict:
        with self._lock:
            self._state.group_name = name
            return self._state.copy()

    def set_enabled_dancers(self, enabled: List[str]) -> Dict:
        with self._lock:
            self._state.enabled_dancers = enabled or []
            return self._state.copy()

    def increment_win(self, name: str) -> Dict:
        with self._lock:
            wins = self._state.win_counts or {}
            key = (name or "").strip()
            if key:
                wins[key] = wins.get(key, 0) + 1
            self._state.win_counts = wins
            return self._state.copy()

    def set_wins_for(self, name: str, wins_value: int) -> Dict:
        with self._lock:
            wins = self._state.win_counts or {}
            key = (name or "").strip()
            if key:
                wins[key] = max(0, int(wins_value))
            self._state.win_counts = wins
            return self._state.copy()

    def _persist_library(self, lib: Dict[str, Any]) -> None:
        if not self._library_path:
            return
        try:
            self._library_path.parent.mkdir(parents=True, exist_ok=True)
            self._library_path.write_text(json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _persist_counts(self, path: Optional[Path], data: Dict[str, int]) -> None:
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _persist_play_counts(self, plays: Dict[str, int]) -> None:
        if not getattr(self, "_plays_path", None):
            return
        try:
            self._plays_path.parent.mkdir(parents=True, exist_ok=True)
            self._plays_path.write_text(json.dumps(plays, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_library(self) -> Dict[str, Any]:
        if not self._library_path or not self._library_path.exists():
            return {}
        try:
            data = json.loads(self._library_path.read_text(encoding="utf-8"))
            # Normalize front_dancer -> front_dancers list
            for k, v in data.items():
                if "front_dancer" in v and "front_dancers" not in v:
                    v["front_dancers"] = [v.pop("front_dancer")] if v.get("front_dancer") else []
                v.setdefault("front_dancers", [])
                v.setdefault("dancers", [])
                v.setdefault("mvp_dancers", [])
                v.setdefault("roles", [])
                v.setdefault("knows_song", [])
            return data
        except Exception:
            return {}

    def _load_play_counts(self) -> Dict[str, int]:
        return self._load_counts(self._plays_path)

    def _load_points_counts(self) -> Dict[str, int]:
        return self._load_counts(self._points_path)

    def _load_counts(self, path: Optional[Path]) -> Dict[str, int]:
        if not path or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): int(v) for k, v in data.items()}
        except Exception:
            return {}
        return {}

    def _load_play_counts(self) -> Dict[str, int]:
        if not getattr(self, "_plays_path", None) or not self._plays_path.exists():
            return {}
        try:
            data = json.loads(self._plays_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): int(v) for k, v in data.items()}
        except Exception:
            return {}
        return {}

    def _clear_roles(self, roles: list, keep_song_id: str, lib: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Ensure each role is unique across songs."""
        lib = lib or self._state.songs.get("library", {}) or {}
        role_set = set(roles or [])
        for sid, meta in lib.items():
            if sid == keep_song_id:
                continue
            existing = set(meta.get("roles", []))
            if existing & role_set:
                meta["roles"] = list(existing - role_set)
                lib[sid] = meta
        return lib

    def _persist_dancers(self, dancers: List[Dict[str, str]]) -> None:
        if not self._dancers_path:
            return
        try:
            self._dancers_path.parent.mkdir(parents=True, exist_ok=True)
            self._dancers_path.write_text(json.dumps(dancers, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_dancers(self) -> List[Dict[str, str]]:
        if not self._dancers_path or not self._dancers_path.exists():
            return []
        try:
            return json.loads(self._dancers_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def increment_play(self, url: str) -> Dict:
        with self._lock:
            if not url:
                return self._state.copy()
            plays = self._state.play_counts or {}
            plays[url] = plays.get(url, 0) + 1
            self._state.play_counts = plays
            self._persist_counts(self._plays_path, plays)
            return self._state.copy()

    def increment_points(self, url: str, amount: int = 1) -> Dict:
        with self._lock:
            if not url or amount <= 0:
                return self._state.copy()
            points = self._state.points_counts or {}
            points[url] = points.get(url, 0) + amount
            self._state.points_counts = points
            self._persist_counts(self._points_path, points)
            return self._state.copy()

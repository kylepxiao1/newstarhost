from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional


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

    def copy(self) -> Dict:
        return asdict(self)


class BattleStateManager:
    def __init__(self, overlay_names) -> None:
        self._state = BattleState(overlay_states={name: True for name in overlay_names})
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


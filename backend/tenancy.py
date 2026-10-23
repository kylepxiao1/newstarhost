"""Per-request dance group context.

The auth middleware puts the logged-in group in `current_group`; `state_manager` (a proxy)
then resolves to that group's BattleStateManager, so every route works on the caller's own
songs/dancers/state without passing the group around explicitly.
"""
from __future__ import annotations

import contextvars
import logging
import threading
from typing import Callable, Optional

from backend.auth import Group

logger = logging.getLogger("tenancy")

current_group: contextvars.ContextVar[Optional[Group]] = contextvars.ContextVar("current_group", default=None)

# Offline bundle: a single local group, no logins.
OFFLINE_GROUP = Group(
    id="offline", username="offline", display_name="", tiktok_handle="",
    session_version=1, overlay_key="", disabled=False,
)


class NoGroupError(RuntimeError):
    pass


def require_group() -> Group:
    group = current_group.get()
    if group is None:
        raise NoGroupError("No logged-in group for this request")
    return group


class GroupRegistry:
    """Lazily loads and caches one state manager per group."""

    def __init__(self, factory: Callable[[Group], object]) -> None:
        self._factory = factory
        self._managers: dict[str, object] = {}
        self._lock = threading.Lock()
        self._loading: dict[str, threading.Lock] = {}

    def get(self, group: Group):
        manager = self._managers.get(group.id)
        if manager is not None:
            return manager
        with self._lock:
            load_lock = self._loading.setdefault(group.id, threading.Lock())
        with load_lock:
            manager = self._managers.get(group.id)
            if manager is None:
                logger.info("Loading group %s (%s)", group.username, group.id)
                manager = self._factory(group)
                self._managers[group.id] = manager
        return manager

    def loaded(self) -> list:
        return list(self._managers.values())

    def flush_all(self, timeout: float = 30.0) -> None:
        for manager in self.loaded():
            try:
                manager.flush(timeout)
            except Exception as exc:
                logger.warning("Flush failed: %s", exc)


class CurrentGroupProxy:
    """Attribute access resolves against the current request's group object from the registry."""

    def __init__(self, registry: GroupRegistry) -> None:
        self._registry = registry

    def __getattr__(self, name: str):
        return getattr(self._registry.get(require_group()), name)

"""Group logins: password hashing, signed session cookies and group lookup.

One login per dance group. A session cookie carries the group id plus the group's
session_version, signed with SESSION_SECRET; bumping session_version (password reset)
logs out every device. Overlays authenticate with the group's overlay_key instead.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from backend.supabase_rest import SupabaseRest
from scripts.utils import _env

SESSION_COOKIE = "nsh_session"
SESSION_MAX_AGE = 180 * 24 * 3600
_GROUP_CACHE_SECONDS = 30.0
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
_PBKDF2_ITERATIONS = 600_000


@dataclass(frozen=True)
class Group:
    id: str
    username: str
    display_name: str
    tiktok_handle: str
    session_version: int
    overlay_key: str
    disabled: bool
    tiktok_id: str = ""
    past_handles: tuple = ()

    def all_handles(self) -> list:
        """Current handle first, then earlier ones (history recorded under old handles)."""
        out = [h for h in [self.tiktok_handle, *self.past_handles] if h]
        return list(dict.fromkeys(out))


def hash_password(password: str) -> str:
    # PBKDF2 is in every Python build (scrypt isn't, e.g. macOS's system Python).
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if parts[0] == "pbkdf2_sha256":
            iterations, salt, expected = int(parts[1]), base64.b64decode(parts[2]), base64.b64decode(parts[3])
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        elif parts[0] == "scrypt":
            salt, expected = base64.b64decode(parts[1]), base64.b64decode(parts[2])
            digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
        else:
            return False
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


_DUMMY_HASH = f"pbkdf2_sha256${_PBKDF2_ITERATIONS}$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def new_overlay_key() -> str:
    return secrets.token_urlsafe(24)


def _session_secret() -> bytes:
    secret = _env("SESSION_SECRET", "").strip()
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must be set (32+ characters) in app.env")
    return secret.encode("utf-8")


def make_session_token(group: Group) -> str:
    payload = f"{group.id}.{group.session_version}.{int(time.time())}"
    sig = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def parse_session_token(token: str) -> Optional[tuple[str, int]]:
    """Returns (group_id, session_version) for a valid, unexpired token."""
    try:
        group_id, version, issued, sig = token.split(".")
        payload = f"{group_id}.{version}.{issued}"
        expected = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if time.time() - int(issued) > SESSION_MAX_AGE:
            return None
        return group_id, int(version)
    except Exception:
        return None


def _to_group(row: dict) -> Group:
    return Group(
        id=str(row["id"]),
        username=str(row["username"]),
        display_name=str(row.get("display_name") or row["username"]),
        tiktok_handle=str(row.get("tiktok_handle") or "").lstrip("@"),
        session_version=int(row.get("session_version") or 1),
        overlay_key=str(row.get("overlay_key") or ""),
        disabled=bool(row.get("disabled")),
        tiktok_id=str(row.get("tiktok_id") or ""),
        past_handles=tuple(str(h).lstrip("@").lower() for h in (row.get("past_handles") or []) if h),
    )


_PUBLIC_COLUMNS = "id,username,display_name,tiktok_handle,session_version,overlay_key,disabled,tiktok_id,past_handles"


class GroupDirectory:
    """Looks up groups in Supabase with a short in-memory cache."""

    def __init__(self, rest: Optional[SupabaseRest] = None) -> None:
        self.rest = rest or SupabaseRest()
        self._lock = threading.Lock()
        self._by_id: dict[str, tuple[float, Optional[Group]]] = {}
        self._by_overlay: dict[str, tuple[float, Optional[Group]]] = {}

    def invalidate(self, group_id: Optional[str] = None) -> None:
        with self._lock:
            if group_id is None:
                self._by_id.clear()
                self._by_overlay.clear()
            else:
                self._by_id.pop(group_id, None)
                self._by_overlay = {k: v for k, v in self._by_overlay.items() if not v[1] or v[1].id != group_id}

    def _cached(self, cache: dict, key: str, loader) -> Optional[Group]:
        now = time.monotonic()
        with self._lock:
            hit = cache.get(key)
            if hit and now - hit[0] < _GROUP_CACHE_SECONDS:
                return hit[1]
        group = loader()
        with self._lock:
            cache[key] = (now, group)
        return group

    def by_id(self, group_id: str) -> Optional[Group]:
        def load():
            row = self.rest.select_one("groups", {"select": _PUBLIC_COLUMNS, "id": f"eq.{group_id}"})
            return _to_group(row) if row else None
        return self._cached(self._by_id, group_id, load)

    def by_overlay_key(self, key: str) -> Optional[Group]:
        if not key or len(key) < 16:
            return None
        def load():
            row = self.rest.select_one("groups", {"select": _PUBLIC_COLUMNS, "overlay_key": f"eq.{key}"})
            return _to_group(row) if row else None
        return self._cached(self._by_overlay, key, load)

    def authenticate(self, username: str, password: str) -> Optional[Group]:
        username = (username or "").strip().lower()
        if not username:
            return None
        row = self.rest.select_one("groups", {"select": _PUBLIC_COLUMNS + ",password_hash", "username": f"eq.{username}"})
        if not row:
            # Spend comparable time so response timing doesn't reveal valid usernames.
            verify_password(password, _DUMMY_HASH)
            return None
        stored = row.get("password_hash") or ""
        # An empty hash means the group has no password (e.g. a shared test account).
        if stored and not verify_password(password, stored):
            return None
        group = _to_group(row)
        return None if group.disabled else group

    def session_group(self, token: str) -> Optional[Group]:
        parsed = parse_session_token(token or "")
        if not parsed:
            return None
        group = self.by_id(parsed[0])
        if not group or group.disabled or group.session_version != parsed[1]:
            return None
        return group

    def rotate_overlay_key(self, group_id: str) -> str:
        key = new_overlay_key()
        self.rest.update("groups", {"id": f"eq.{group_id}"}, {"overlay_key": key, "updated_at": datetime.now(timezone.utc).isoformat()})
        self.invalidate(group_id)
        return key

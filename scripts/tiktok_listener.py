import asyncio
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Optional

import httpx
from TikTokLive import TikTokLiveClient
from TikTokLive import events as ttevents
from TikTokLive.client.errors import WebcastBlocked200Error
from TikTokLive.events import proto_events
from TikTokLive.client.web.web_settings import WebDefaults
from event_store import SupabaseEventStore
from utils import (
    _extract_handle,
    _extract_recipient_from_describe,
    _get_any,
    _get_attr_any,
    _is_placeholder_obj,
    _is_valid_value,
    _safe_event_user,
)

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
    for name in (".env",):
        candidate = repo_root / name
        if candidate.exists() and candidate.is_file():
            return _load_env_file(candidate)
    return {}


ENV = _load_env_from_config()


def _env(key: str, default: str = "") -> str:
    return ENV.get(key, default)


def _env_flag(key: str, default: bool = False) -> bool:
    value = _env(key, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}

API_BASE = _env("BATTLE_API")
TIKTOK_USERNAMES = _env("TIKTOK_USERNAMES")
LOG_FILE = _env("LOG_FILE")
EULERSTREAM_API_KEY = _env("EULERSTREAM_API_KEY")
EULERSTREAM_SIGN_URL = _env("EULERSTREAM_SIGN_URL")
WHITELIST_AUTHENTICATED_SESSION_ID_HOST = _env("WHITELIST_AUTHENTICATED_SESSION_ID_HOST")
TIKTOK_COOKIES_FILE = _env("TIKTOK_COOKIES_FILE")
TIKTOK_DEVICE_ID_FILE = _env("TIKTOK_DEVICE_ID_FILE")
SUPABASE_PROJECT_ID = _env("SUPABASE_PROJECT_ID")
SUPABASE_SECRET_KEY = _env("SUPABASE_SECRET_KEY")
SUPABASE_DEBUG = _env_flag("SUPABASE_DEBUG", False)
DEFAULT_SLOT_ONE = "Performer One"
DEFAULT_SLOT_TWO = "Performer Two"
ENABLE_SCORE_UPDATES = _env_flag("ENABLE_SCORE_UPDATES", False)

if WHITELIST_AUTHENTICATED_SESSION_ID_HOST:
    os.environ.setdefault(
        "WHITELIST_AUTHENTICATED_SESSION_ID_HOST",
        WHITELIST_AUTHENTICATED_SESSION_ID_HOST,
    )

_log_handlers = [logging.StreamHandler()]
if LOG_FILE:
    _log_handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger("tiktok-listener")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
if SUPABASE_DEBUG:
    logger.setLevel(logging.DEBUG)


COOKIE_ENV_MAP = {
    "sessionid": "TIKTOK_SESSIONID",
    "sessionid_ss": "TIKTOK_SESSIONID_SS",
    "sid_tt": "TIKTOK_SID_TT",
    "tt-target-idc": "TIKTOK_TT_TARGET_IDC",
    "ttwid": "TIKTOK_TTWID",
    "msToken": "TIKTOK_MS_TOKEN",
    "tt_chain_token": "TIKTOK_TT_CHAIN_TOKEN",
}


def _payload(evt) -> dict:
    try:
        if hasattr(evt, "as_dict"):
            return evt.as_dict()
        return vars(evt)
    except Exception:
        return {"repr": repr(evt)}


def looks_like_battle_start(comment: Optional[str], gift_name: Optional[str]) -> bool:
    text = (comment or "").lower()
    if "!battle" in text or "start battle" in text:
        return True
    if gift_name and "battle" in gift_name.lower():
        return True
    return False


def looks_like_battle_end(comment: Optional[str], gift_name: Optional[str]) -> bool:
    text = (comment or "").lower()
    if "!end" in text or "end battle" in text or "gg" == text.strip():
        return True
    if gift_name and "whistle" in gift_name.lower():
        return True
    return False


def _load_netscape_cookies(path: Path) -> dict:
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    cookies = {}
    for cookie in jar:
        if "tiktok" not in cookie.domain:
            continue
        cookies[cookie.name] = cookie.value
    return cookies


def _gather_tiktok_cookies() -> dict:
    cookies: dict[str, str] = {}
    if TIKTOK_COOKIES_FILE:
        path = Path(TIKTOK_COOKIES_FILE).expanduser()
        if path.exists():
            try:
                cookies.update(_load_netscape_cookies(path))
                logger.info("Loaded TikTok cookies from %s", path)
            except Exception as exc:
                logger.warning("Failed to load cookies file %s: %s", path, exc)
        else:
            logger.warning("TikTok cookies file not found: %s", path)
    for cookie_key, env_name in COOKIE_ENV_MAP.items():
        val = _env(env_name, "")
        if val:
            cookies[cookie_key] = val
    return cookies


def _load_device_id() -> Optional[str]:
    env_val = _env("TIKTOK_DEVICE_ID", "")
    if env_val:
        return env_val.strip()
    if TIKTOK_DEVICE_ID_FILE:
        path = Path(TIKTOK_DEVICE_ID_FILE).expanduser()
        if path.exists():
            try:
                return path.read_text(encoding="utf-8").strip()
            except Exception as exc:
                logger.warning("Failed to read device id file %s: %s", path, exc)
    return None


def _persist_device_id(device_id: str) -> None:
    if not device_id or not TIKTOK_DEVICE_ID_FILE:
        return
    path = Path(TIKTOK_DEVICE_ID_FILE).expanduser()
    try:
        path.write_text(str(device_id), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to persist device id to %s: %s", path, exc)


def _pin_device_id(client: TikTokLiveClient, device_id: Optional[str]) -> Optional[str]:
    if not device_id:
        return None
    try:
        device_int = int(device_id)
    except ValueError:
        logger.warning("Invalid TIKTOK_DEVICE_ID (expected int), ignoring: %s", device_id)
        return None
    try:
        client.web.generate_device_id = lambda: device_int  # type: ignore[attr-defined]
        client.web.params["device_id"] = device_int
        return str(device_int)
    except Exception as exc:
        logger.warning("Unable to lock device id: %s", exc)
        return None


def _format_user(name: str, handle: str) -> str:
    name = name.strip() if isinstance(name, str) else ""
    handle = handle.strip("@") if isinstance(handle, str) else ""
    if name and handle and name.lower() != handle.lower():
        return f"{name} (@{handle})"
    if handle:
        return f"@{handle}"
    return name


def _format_handle(name: str, handle: str) -> str:
    """
    Prefer a handle; fallback to name; always prefix with @ when a handle or name exists.
    """
    try:
        handle_val = handle if isinstance(handle, str) else str(handle or "")
        name_val = name if isinstance(name, str) else str(name or "")
    except Exception:
        handle_val = ""
        name_val = ""
    handle_val = handle_val or ""
    name_val = name_val or ""
    chosen = handle_val.strip() or name_val.strip()
    if not chosen:
        return ""
    chosen = chosen.lstrip("@")
    return f"@{chosen}"


def _normalize_user_id(val: str) -> str:
    try:
        s = val if isinstance(val, str) else str(val or "")
    except Exception:
        return ""
    if s.startswith("<object object") and "object at" in s:
        return ""
    return s.strip().lstrip("@").lower()


def _coerce_int(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text or _is_placeholder_obj(text):
        return None
    try:
        return int(float(text))
    except Exception:
        return None


_HOST_TOKENS = {"host", "the host", "creator", "streamer"}


def _clean_recipient(primary, fallback="") -> str:
    candidate = ""
    if isinstance(primary, str):
        candidate = primary.strip()
    else:
        candidate = _extract_handle(primary)
    if candidate:
        lowered = candidate.strip().lower()
        if lowered in _HOST_TOKENS:
            candidate = ""
        elif not _is_placeholder_obj(candidate):
            return candidate
    if isinstance(fallback, str):
        candidate = fallback.strip()
    else:
        candidate = _extract_handle(fallback)
    if candidate:
        lowered = candidate.strip().lower()
        if lowered in _HOST_TOKENS:
            return ""
        if not _is_placeholder_obj(candidate):
            return candidate
    return ""


class TikTokLiveListener:
    def __init__(self, username: str, api_base: str, event_store: SupabaseEventStore) -> None:
        self.username = username
        self.api_base = api_base.rstrip("/")
        self.http = httpx.AsyncClient(timeout=10)
        self._event_store = event_store
        self._last_start = datetime.min.replace(tzinfo=timezone.utc)
        self._last_end = datetime.min.replace(tzinfo=timezone.utc)
        self._cooldown = timedelta(seconds=30)
        self._last_scores = {"slot_one": 0, "slot_two": 0}
        self._slots = {"slot_one": DEFAULT_SLOT_ONE, "slot_two": DEFAULT_SLOT_TWO}
        self._score_by_id: dict[str, int] = {}
        self._base_backoff = 5
        self._max_backoff = 300
        self._rate_limit_backoff = 300  # 5 minutes
        self._offline_backoff = 60
        self._not_found_backoff = 900
        self._last_rate_limit_log = datetime.min.replace(tzinfo=timezone.utc)
        self._rate_limit_logged = False
        self._last_offline_log = datetime.min.replace(tzinfo=timezone.utc)
        self._offline_logged = False
        self._last_not_found_log = datetime.min.replace(tzinfo=timezone.utc)
        self._not_found_logged = False
        self._last_parse_error_log = datetime.min.replace(tzinfo=timezone.utc)
        # dedupe cache (type,id) -> timestamp
        self._seen = {}
        # combo tracking: key -> (last_combo_count, last_seen_ts)
        self._combo_state: dict[tuple, tuple[int, float]] = {}
        self._combo_ttl = timedelta(minutes=10)
        self._live_check_interval_min = 60
        self._live_check_interval_max = 120
        self._next_live_check_at = 0.0
        self._last_live_status: Optional[bool] = None
        self._cookies = _gather_tiktok_cookies()
        self._device_id = _load_device_id()

        # Apply signer defaults if available
        if WebDefaults:
            WebDefaults.tiktok_webcast_url = "https://webcast.us.tiktok.com/webcast"
            if EULERSTREAM_API_KEY:
                try:
                    WebDefaults.tiktok_sign_api_key = EULERSTREAM_API_KEY
                except Exception:
                    pass
            if EULERSTREAM_SIGN_URL:
                try:
                    WebDefaults.tiktok_sign_url = EULERSTREAM_SIGN_URL
                except Exception:
                    pass

            if self._cookies:
                try:
                    WebDefaults.web_client_cookies = {**WebDefaults.web_client_cookies, **self._cookies}
                except Exception as exc:
                    logger.warning("Failed to merge TikTok cookies into defaults: %s", exc)
            ms_token = self._cookies.get("msToken") if isinstance(self._cookies, dict) else None
            if ms_token:
                try:
                    WebDefaults.web_client_params["msToken"] = ms_token
                except Exception:
                    pass

        self.client = TikTokLiveClient(unique_id=username)
        self._apply_client_cookies()
        if not self._device_id:
            try:
                self._device_id = str(self.client.web.generate_device_id())
            except Exception:
                self._device_id = None
        pinned = _pin_device_id(self.client, self._device_id)
        if pinned:
            self._device_id = pinned
            _persist_device_id(pinned)
        self._wire_events()

    def _cookie_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        try:
            jar = getattr(self.client.web.cookies, "jar", None)
            iterable = jar if jar is not None else self.client.web.cookies
            for cookie in iterable:
                try:
                    counts[cookie.name] = counts.get(cookie.name, 0) + 1
                except Exception:
                    continue
        except Exception:
            return counts
        return counts

    def _log_cookie_snapshot(self, label: str) -> None:
        try:
            counts = self._cookie_counts()
            if not counts:
                logger.debug("Cookie jar snapshot (%s): empty or unavailable", label)
                return
            dups = {k: v for k, v in counts.items() if v > 1}
            if dups:
                logger.warning("Cookie jar duplicates (%s): %s", label, dups)
                if logger.isEnabledFor(logging.DEBUG):
                    jar = getattr(self.client.web.cookies, "jar", None)
                    iterable = jar if jar is not None else self.client.web.cookies
                    for name in dups:
                        details = [
                            f"{getattr(c, 'domain', '')}{getattr(c, 'path', '')}"
                            for c in iterable
                            if getattr(c, "name", None) == name
                        ]
                        logger.debug("Cookie '%s' entries: %s", name, ", ".join(details))
            else:
                logger.debug("Cookie jar snapshot (%s): %s", label, counts)
        except Exception as exc:
            logger.debug("Failed cookie snapshot (%s): %s", label, exc)

    def _apply_client_cookies(self) -> None:
        if not self._cookies:
            return
        self._log_cookie_snapshot("before_apply")
        # Reset cookie jar to avoid duplicated defaults from WebDefaults
        try:
            self.client.web.cookies = httpx.Cookies()
        except Exception:
            pass
        session_id = self._cookies.get("sessionid") or self._cookies.get("sid_tt")
        tt_target_idc = self._cookies.get("tt-target-idc")
        try:
            if session_id:
                self.client.web.set_session(session_id, tt_target_idc)
        except Exception as exc:
            logger.warning("Failed to apply TikTok session cookie: %s", exc)
        for key, val in self._cookies.items():
            if key in ("sessionid", "sessionid_ss", "sid_tt", "tt-target-idc"):
                continue
            try:
                self.client.web.cookies.set(key, val, domain=".tiktok.com", path="/")
            except Exception:
                try:
                    self.client.web.cookies.set(key, val)
                except Exception:
                    logger.debug("Could not set cookie %s", key)
        if self._cookies.get("msToken"):
            try:
                self.client.web.params["msToken"] = self._cookies["msToken"]
            except Exception:
                pass
        self._log_cookie_snapshot("after_apply")

    def _make_event_key(self, evt, etype: str) -> Optional[str]:
        payload = _payload(evt)
        # Prefer explicit ids
        id_keys = ("event_id", "message_id", "id", "msg_id", "cid")
        if isinstance(payload, dict):
            for k in id_keys:
                v = payload.get(k)
                if v:
                    return f"{etype}:{v}"
        else:
            for k in id_keys:
                v = getattr(evt, k, None)
                if v:
                    return f"{etype}:{v}"

        # comment-specific dedupe: include sender + text
        if etype == "comment":
            comment_text = ""
            sender = ""
            if isinstance(payload, dict):
                comment_text = payload.get("comment") or payload.get("content") or ""
                if "user" in payload and isinstance(payload["user"], dict):
                    sender = payload["user"].get("unique_id") or payload["user"].get("display_id") or ""
            else:
                comment_text = getattr(evt, "comment", "") or getattr(evt, "content", "") or ""
                user_obj = getattr(evt, "user", None)
                if user_obj:
                    sender = getattr(user_obj, "unique_id", "") or getattr(user_obj, "display_id", "") or ""
            if comment_text:
                try:
                    text_val = comment_text if isinstance(comment_text, str) else str(comment_text)
                    snippet = text_val.strip()[:64]
                except Exception:
                    snippet = ""
                if snippet:
                    return f"{etype}:{sender}:{snippet}"

        # fallback: ts + sender
        ts = payload.get("timestamp") or payload.get("create_time") if isinstance(payload, dict) else None
        sender = ""
        if isinstance(payload, dict):
            if "user" in payload and isinstance(payload["user"], dict):
                sender = payload["user"].get("unique_id") or payload["user"].get("display_id") or ""
            elif "user_id" in payload:
                sender = str(payload.get("user_id"))
        if ts:
            return f"{etype}:{ts}:{sender}"
        return None

    def _is_duplicate(self, evt, etype: str) -> bool:
        key = self._make_event_key(evt, etype)
        if not key:
            return False
        now = datetime.now().timestamp()
        # purge old
        drop_before = now - 30
        for k in list(self._seen.keys()):
            if self._seen[k] < drop_before:
                self._seen.pop(k, None)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    def _schedule_next_live_check(self, delay: Optional[float] = None) -> float:
        if delay is None:
            delay = random.uniform(self._live_check_interval_min, self._live_check_interval_max)
        self._next_live_check_at = time.monotonic() + max(1.0, float(delay))
        return delay

    async def _check_live_status(self) -> Optional[bool]:
        now = time.monotonic()
        if now < self._next_live_check_at:
            return self._last_live_status
        try:
            status = await self.client.is_live()
            self._last_live_status = bool(status)
            self._schedule_next_live_check()
            return self._last_live_status
        except Exception as exc:
            msg = str(exc).lower()
            if "rate_limit" in msg or "too many connections" in msg:
                now = datetime.now(timezone.utc)
                if not self._rate_limit_logged or (now - self._last_rate_limit_log) > timedelta(minutes=10):
                    logger.warning(
                        "TikTok rate limit during live status check. Backing off for %s seconds.",
                        self._rate_limit_backoff,
                    )
                    self._rate_limit_logged = True
                    self._last_rate_limit_log = now
                self._schedule_next_live_check(self._rate_limit_backoff)
            else:
                self._schedule_next_live_check(self._live_check_interval_max)
            self._last_live_status = None
            return None

    async def _sleep_with_jitter(self, seconds: float) -> None:
        if seconds <= 0:
            return
        jitter = random.uniform(0.8, 1.2)
        delay = max(1.0, seconds * jitter)
        await asyncio.sleep(delay)

    def _combo_key(self, payload: dict) -> Optional[tuple]:
        gid = _get_any(payload, "group_id", "groupId")
        if gid:
            gid_str = str(gid)
            if not _is_placeholder_obj(gid_str):
                return ("group", gid_str)
        msg_id = _get_any(payload, "msg_id", "message_id", "event_id")
        if msg_id:
            return ("msg", str(msg_id))
        return None

    def _combo_delta(self, payload: dict, combo_count: Optional[int]) -> int:
        count = combo_count if combo_count is not None else 1
        key = self._combo_key(payload)
        if not key:
            return count
        now = datetime.now(timezone.utc).timestamp()
        drop_before = now - self._combo_ttl.total_seconds()
        for k, (_, ts) in list(self._combo_state.items()):
            if ts < drop_before:
                self._combo_state.pop(k, None)
        prev_count, _ = self._combo_state.get(key, (0, now))
        if count < prev_count:
            delta = count
        else:
            delta = count - prev_count
        self._combo_state[key] = (count, now)
        return max(0, delta)

    def _wire_events(self) -> None:
        try:
            self.client.remove_all_listeners()
        except Exception:
            pass

        @self.client.on(ttevents.ConnectEvent)
        async def on_connect(_: ttevents.ConnectEvent) -> None:
            host = None
            try:
                host_obj = getattr(self.client, "room_info", None)
                host_obj = getattr(host_obj, "host", None)
                host = getattr(host_obj, "display_id", None) or getattr(host_obj, "unique_id", None)
            except Exception:
                host = None
            logger.info("Connected to TikTok LIVE as %s", host or self.client.unique_id or "unknown")
            self._offline_logged = False
            self._not_found_logged = False
            self._rate_limit_logged = False

        @self.client.on(ttevents.DisconnectEvent)
        async def on_disconnect(_: ttevents.DisconnectEvent) -> None:
            logger.warning("Disconnected from TikTok LIVE. Reconnecting...")

        @self.client.on(ttevents.LinkMicBattleEvent)
        async def on_battle(event: ttevents.LinkMicBattleEvent) -> None:
            if self._is_duplicate(event, "battle"):
                return
            logger.info("LinkMicBattleEvent")
            self._last_scores = {"slot_one": 0, "slot_two": 0}
            await self.trigger_start("linkmic_battle_event")

        @self.client.on(ttevents.LinkmicBattleNoticeEvent)
        async def on_battle_notice(event: ttevents.LinkmicBattleNoticeEvent) -> None:
            if self._is_duplicate(event, "battle_notice"):
                return
            logger.info("LinkmicBattleNoticeEvent")
            await self.trigger_start("linkmic_battle_notice")

        @self.client.on(ttevents.LinkMicBattleVictoryLapEvent)
        async def on_battle_victory(event: ttevents.LinkMicBattleVictoryLapEvent) -> None:
            if self._is_duplicate(event, "battle_victory"):
                return
            logger.info("LinkMicBattleVictoryLapEvent")
            await self.trigger_end("linkmic_battle_victory")

        @self.client.on(ttevents.LinkMicArmiesEvent)
        async def on_armies(event: ttevents.LinkMicArmiesEvent) -> None:
            if self._is_duplicate(event, "armies"):
                return
            logger.info("LinkMicArmiesEvent")
            data = _payload(event)
            armies = data.get("armies") or data.get("army_list") or data.get("battle_armies") or []
            if isinstance(armies, dict):
                armies = armies.get("armies") or armies.get("army_list") or list(armies.values())
            slot_one_score = self._last_scores["slot_one"]
            slot_two_score = self._last_scores["slot_two"]
            if isinstance(armies, list):
                if len(armies) > 0:
                    slot_one_score = armies[0].get("points") or armies[0].get("score") or slot_one_score
                if len(armies) > 1:
                    slot_two_score = armies[1].get("points") or armies[1].get("score") or slot_two_score
            # If we see armies before an explicit battle start, treat this as the start signal.
            now = datetime.now(timezone.utc)
            if now - self._last_start > self._cooldown:
                await self.trigger_start("armies_event")
            await self._update_scores(slot_one_score, slot_two_score)

        @self.client.on(ttevents.LinkMicBattlePunishFinishEvent)
        async def on_battle_punish_finish(event: ttevents.LinkMicBattlePunishFinishEvent) -> None:
            if self._is_duplicate(event, "battle_end"):
                return
            logger.info("LinkMicBattlePunishFinishEvent")
            await self.trigger_end("linkmic_battle_punish_finish")

        @self.client.on(ttevents.LinkStateEvent)
        async def on_link_state(event: ttevents.LinkStateEvent) -> None:
            if self._is_duplicate(event, "link_state"):
                return
            payload = _payload(event)
            state_val = ""
            try:
                state_val = str(payload.get("state") or payload.get("link_state") or "")
            except Exception:
                state_val = ""
            state_lower = state_val.lower()
            logger.info("LinkStateEvent: %s", state_val)
            if "battle" in state_lower and ("start" in state_lower or "enter" in state_lower or "begin" in state_lower):
                await self.trigger_start("link_state")
            elif "battle" in state_lower and ("end" in state_lower or "finish" in state_lower or "exit" in state_lower):
                await self.trigger_end("link_state")

        @self.client.on(ttevents.ControlEvent)
        async def on_control(event: ttevents.ControlEvent) -> None:
            if self._is_duplicate(event, "control"):
                return
            payload = _payload(event)
            action = ""
            try:
                action = str(payload.get("action") or "")
            except Exception:
                action = ""
            action_lower = action.lower()
            logger.info("ControlEvent: %s", action)
            if "battle" in action_lower and ("start" in action_lower or "begin" in action_lower):
                await self.trigger_start("control")
            elif "battle" in action_lower and ("end" in action_lower or "finish" in action_lower or "stop" in action_lower):
                await self.trigger_end("control")

        @self.client.on(ttevents.GiftEvent)
        async def on_gift(event: ttevents.GiftEvent) -> None:
            if self._is_duplicate(event, "gift"):
                return
            payload = _payload(event)
            raw_id = await self._event_store.log_event("gift", payload, self.username)
            await self._sync_slots()
            gift_name = ""
            gift_amount = ""
            gift_from = ""
            gift_to = ""
            gift_from_handle = ""
            gift_to_handle = ""
            gift_value = 0
            gift_value_raw = None
            gift_value_log_total = None
            combo_delta_computed = False
            repeat_count = None
            combo_count = None
            diamond_count = None
            gift_streakable = False
            gift_streaking = False
            try:
                if isinstance(payload, dict):
                    gift = payload.get("gift") or {}
                    gift_name = gift.get("name") or gift.get("describe") or gift.get("id") or ""
                    if "user" in payload and isinstance(payload["user"], dict):
                        gift_from = (
                            payload["user"].get("unique_id")
                            or payload["user"].get("display_id")
                            or payload["user"].get("nickname")
                            or gift_from
                        )
                        gift_from_handle = (
                            payload["user"].get("unique_id")
                            or payload["user"].get("display_id")
                            or payload["user"].get("username")
                            or gift_from_handle
                        )
                    if "to_user" in payload:
                        tu_handle = _extract_handle(payload.get("to_user"))
                        if tu_handle:
                            gift_to_handle = gift_to_handle or tu_handle
                            gift_to = gift_to or tu_handle
                    if not gift_to and payload.get("to_member_nickname"):
                        gift_to = payload.get("to_member_nickname")
                    if not gift_to and payload.get("to_member_id"):
                        gift_to = str(payload.get("to_member_id"))
                    if not gift_to and payload.get("to_member_id_int"):
                        gift_to = str(payload.get("to_member_id_int"))
                    if "receiver" in payload and isinstance(payload["receiver"], dict):
                        gift_to = (
                            payload["receiver"].get("unique_id")
                            or payload["receiver"].get("display_id")
                            or payload["receiver"].get("nickname")
                            or gift_to
                        )
                        gift_to_handle = gift_to_handle or _extract_handle(payload.get("receiver"))
                    elif "to_user" in payload:
                        tu_handle = _extract_handle(payload.get("to_user"))
                        if tu_handle:
                            gift_to = gift_to or tu_handle
                            gift_to_handle = gift_to_handle or tu_handle
                    repeat_count_raw = (
                        payload.get("repeat_count")
                        or payload.get("repeatCount")
                        or payload.get("repeatEnd")
                        or payload.get("repeat_end")
                        or gift.get("repeat_count")
                    )
                    combo_count_raw = payload.get("combo_count") or payload.get("comboCount")
                    diamond_count_raw = (
                        payload.get("diamond_count")
                        or gift.get("diamond_count")
                        or gift.get("diamonds")
                        or gift.get("diamond_cost")
                    )
                    repeat_count = _coerce_int(repeat_count_raw)
                    combo_count = _coerce_int(combo_count_raw)
                    diamond_count = _coerce_int(diamond_count_raw)
                    if repeat_count is not None and diamond_count is not None:
                        gift_amount = f"{repeat_count} x {diamond_count}"
                    elif diamond_count is not None:
                        gift_amount = str(diamond_count)
                    if diamond_count is not None:
                        combo_total = combo_count if combo_count is not None else repeat_count
                        raw_count = combo_total if combo_total is not None else 1
                        gift_value_raw = int(diamond_count) * int(raw_count)
                        delta_count = self._combo_delta(payload, combo_total)
                        gift_value = int(diamond_count) * int(delta_count)
                        combo_delta_computed = True
                if hasattr(event, "gift") and hasattr(event.gift, "streakable"):
                    gift_streakable = bool(getattr(event.gift, "streakable", False))
                if hasattr(event, "streaking"):
                    gift_streaking = bool(getattr(event, "streaking", False))
                if hasattr(event, "gift") and hasattr(event.gift, "name"):
                    gift_name = gift_name or event.gift.name
                if hasattr(event, "gift") and hasattr(event.gift, "diamond_count"):
                    event_diamond = _coerce_int(getattr(event.gift, "diamond_count"))
                    if event_diamond is not None:
                        gift_amount = gift_amount or str(event_diamond)
                        if diamond_count is None:
                            diamond_count = event_diamond
                        if gift_value_raw is None:
                            gift_value_raw = int(event_diamond)
                event_repeat = _coerce_int(getattr(event, "repeat_count", None))
                if repeat_count is None and event_repeat is not None:
                    repeat_count = event_repeat
                event_combo = _coerce_int(getattr(event, "combo_count", None))
                if combo_count is None and event_combo is not None:
                    combo_count = event_combo
                if diamond_count is not None:
                    combo_total = combo_count if combo_count is not None else repeat_count
                    raw_count = combo_total if combo_total is not None else 1
                    if gift_value_raw is None:
                        gift_value_raw = int(diamond_count) * int(raw_count)
                    if not combo_delta_computed:
                        delta_count = self._combo_delta(payload, combo_total)
                        gift_value = int(diamond_count) * int(delta_count)
                        combo_delta_computed = True
                if hasattr(event, "gift") and hasattr(event.gift, "id") and not gift_name:
                    gift_name = str(getattr(event.gift, "id"))
                if hasattr(event, "user") and event.user:
                    gift_from = (
                        gift_from
                        or getattr(event.user, "unique_id", "")
                        or getattr(event.user, "display_id", "")
                        or getattr(event.user, "nickname", "")
                    )
                    gift_from_handle = (
                        getattr(event.user, "unique_id", "")
                        or getattr(event.user, "display_id", "")
                        or getattr(event.user, "username", "")
                        or gift_from_handle
                    )
                if hasattr(event, "receiver") and event.receiver:
                    gift_to = (
                        gift_to
                        or getattr(event.receiver, "unique_id", "")
                        or getattr(event.receiver, "display_id", "")
                        or getattr(event.receiver, "nickname", "")
                    )
                    if isinstance(event.receiver, object):
                        logger.info("Gift receiver object: %r", event.receiver)
                    gift_to_handle = gift_to_handle or _extract_handle(event.receiver)
                if not gift_to and hasattr(event, "to_user") and event.to_user:
                    tu_handle = _extract_handle(event.to_user)
                    gift_to = gift_to or tu_handle
                    gift_to_handle = gift_to_handle or tu_handle
                if not gift_to:
                    desc = ""
                    if isinstance(payload, dict):
                        desc = str(payload.get("describe") or "")
                    if desc and "gifted the host" in desc.lower():
                        gift_to = self.username or ""
                    parsed_recipient = _extract_recipient_from_describe(desc)
                    if parsed_recipient:
                        gift_to = gift_to or parsed_recipient
                        gift_to_handle = gift_to_handle or _normalize_user_id(parsed_recipient)
            except Exception:
                pass
            should_log = True
            if gift_streakable:
                should_log = not gift_streaking
            if should_log and gift_value_raw is not None:
                gift_value_log_total = gift_value_raw
            elif should_log and diamond_count is not None:
                gift_value_log_total = int(diamond_count)
            if should_log and gift_value_log_total is not None and gift_value_raw is None:
                gift_value_raw = gift_value_log_total
            display_to = _clean_recipient(gift_to, gift_to_handle) or (self.username or "host")
            logger.info(
                "Gift event: %s %s from %s to %s",
                gift_name or "Unknown gift",
                gift_amount or "",
                _format_handle(gift_from, gift_from_handle) or "unknown",
                _format_handle(display_to, display_to) or self.username or "host",
            )
            name = event.gift.name if hasattr(event, "gift") else None
            await self._maybe_trigger(comment=None, gift_name=name)
            if gift_value:
                recipient = _clean_recipient(gift_to, gift_to_handle)
                if not recipient:
                    recipient = self.username or ""
                await self._score_gift(gift_value, recipient)
            if should_log:
                if raw_id is not None:
                    await self._event_store.log_gift(
                        payload,
                        event,
                        self.username,
                        gift_value_raw,
                        gift_value_log_total,
                        raw_id=raw_id,
                    )
            logger.info("Current battle score: %s", self._score_by_id)

        @self.client.on(ttevents.CommentEvent)
        async def on_comment(event: ttevents.CommentEvent) -> None:
            if self._is_duplicate(event, "comment"):
                return
            payload = _payload(event)
            raw_id = await self._event_store.log_event("comment", payload, self.username)
            commenter = ""
            try:
                commenter = _extract_handle(payload.get("user_info")) if isinstance(payload, dict) else ""
                if not commenter:
                    commenter = _extract_handle(_safe_event_user(event))
                if not commenter:
                    commenter = _extract_handle(getattr(event, "user_info", None))
                if not commenter and isinstance(payload, dict):
                    commenter = (
                        _extract_handle(payload.get("user"))
                        or _extract_handle(payload.get("from_user"))
                    )
            except Exception:
                commenter = ""
            commenter = _format_handle(commenter, commenter)
            logger.info("Comment event: %s (by %s)", getattr(event, "comment", None), commenter or "unknown")
            if raw_id is not None:
                await self._event_store.log_comment(payload, event, self.username, raw_id=raw_id)
            text = event.comment or ""
            if text.startswith("!battle"):
                await self.trigger_start("command")
            elif text.startswith("!end"):
                await self.trigger_end("command")
            elif text.startswith("!slots"):
                parts = text.replace("!slots", "", 1).strip().split("|")
                first = parts[0].strip() if parts and parts[0].strip() else DEFAULT_SLOT_ONE
                second = parts[1].strip() if len(parts) > 1 and parts[1].strip() else DEFAULT_SLOT_TWO
                await self.import_slots(first, second)
            else:
                await self._maybe_trigger(comment=text, gift_name=None)

        @self.client.on(ttevents.LikeEvent)
        async def on_like(event: ttevents.LikeEvent) -> None:
            if self._is_duplicate(event, "like"):
                return
            payload = _payload(event)
            raw_id = await self._event_store.log_event("like", payload, self.username)
            if raw_id is not None:
                await self._event_store.log_like(payload, event, self.username, raw_id=raw_id)
            return

    async def _maybe_trigger(self, comment: Optional[str], gift_name: Optional[str]) -> None:
        now = datetime.now(timezone.utc)
        if looks_like_battle_start(comment, gift_name) and now - self._last_start > self._cooldown:
            await self.trigger_start("heuristic")
        elif looks_like_battle_end(comment, gift_name) and now - self._last_end > self._cooldown:
            await self.trigger_end("heuristic")

    async def trigger_start(self, reason: str) -> None:
        self._last_start = datetime.now(timezone.utc)
        self._last_scores = {"slot_one": 0, "slot_two": 0}
        self._score_by_id = {}
        logger.info("Triggering battle start (%s)", reason)
        await self._safe_post(f"{self.api_base}/battle/start", {})
        await self._sync_slots()

    async def trigger_end(self, reason: str) -> None:
        self._last_end = datetime.now(timezone.utc)
        logger.info("Triggering battle end (%s)", reason)
        await self._safe_post(f"{self.api_base}/battle/end", {})
        await self._sync_slots()
        self._score_by_id = {}

    async def import_slots(self, slot_one: Optional[str], slot_two: Optional[str]) -> None:
        ok = await self._safe_post(f"{self.api_base}/battle/slots/import", {"slot_one": slot_one, "slot_two": slot_two})
        if ok:
            logger.info("Imported slots: %s vs %s", slot_one, slot_two)
            self._slots["slot_one"] = slot_one or self._slots["slot_one"]
            self._slots["slot_two"] = slot_two or self._slots["slot_two"]
        await self._sync_slots()

    async def _update_scores(self, slot_one_score: int, slot_two_score: int) -> dict:
        delta_one = max(0, int(slot_one_score) - self._last_scores.get("slot_one", 0))
        delta_two = max(0, int(slot_two_score) - self._last_scores.get("slot_two", 0))
        self._last_scores["slot_one"] = int(slot_one_score)
        self._last_scores["slot_two"] = int(slot_two_score)
        if ENABLE_SCORE_UPDATES:
            if delta_one:
                await self._safe_post(f"{self.api_base}/score/slot_one/add", {"amount": delta_one})
            if delta_two:
                await self._safe_post(f"{self.api_base}/score/slot_two/add", {"amount": delta_two})
        return self._last_scores

    def _slot_for_recipient(self, recipient: str) -> str:
        try:
            rec_val = recipient if isinstance(recipient, str) else str(recipient or "")
        except Exception:
            rec_val = ""
        rec = (rec_val or "").strip().lower().lstrip("@")
        if _is_placeholder_obj(rec_val):
            rec = ""
        if not rec:
            return "slot_one"
        for key, name in self._slots.items():
            if not name:
                continue
            n = name.strip().lower().lstrip("@")
            if not n:
                continue
            if rec == n:
                return key
        for key, name in self._slots.items():
            if not name:
                continue
            n = name.strip().lower().lstrip("@")
            if n and (rec in n or n in rec):
                return key
        return "slot_one"

    async def _score_gift(self, amount: int, recipient: str) -> None:
        if amount <= 0:
            return
        slot = self._slot_for_recipient(recipient)
        await self._safe_post(f"{self.api_base}/score/{slot}/add", {"amount": amount})
        self._last_scores[slot] = self._last_scores.get(slot, 0) + amount
        rid = _normalize_user_id(recipient)
        if not rid:
            slot_name = self._slots.get(slot) or ""
            rid = _normalize_user_id(slot_name) or slot
        if rid:
            self._score_by_id[rid] = self._score_by_id.get(rid, 0) + amount

    async def _sync_slots(self) -> None:
        """
        Pull current slots from backend /state to improve gift->slot mapping.
        """
        try:
            resp = await self.http.get(f"{self.api_base}/state", timeout=5)
            data = resp.json()
            slot_one_name = ""
            slot_two_name = ""
            if isinstance(data, dict):
                slot_one = data.get("slot_one") or data.get("slotOne") or {}
                slot_two = data.get("slot_two") or data.get("slotTwo") or {}
                if isinstance(slot_one, dict):
                    slot_one_name = slot_one.get("name") or slot_one.get("slot_one") or slot_one.get("slotOne") or ""
                if isinstance(slot_two, dict):
                    slot_two_name = slot_two.get("name") or slot_two.get("slot_two") or slot_two.get("slotTwo") or ""
                # fallbacks
                slot_one_name = slot_one_name or data.get("slot_one_name") or data.get("slotOneName") or slot_one_name
                slot_two_name = slot_two_name or data.get("slot_two_name") or data.get("slotTwoName") or slot_two_name
            if slot_one_name:
                self._slots["slot_one"] = slot_one_name
            if slot_two_name:
                self._slots["slot_two"] = slot_two_name
            if slot_one_name or slot_two_name:
                logger.info("Synced slots from backend: %s vs %s", self._slots["slot_one"], self._slots["slot_two"])
        except Exception as exc:
            logger.debug("Failed to sync slots: %s", exc)

    async def run(self) -> None:
        backoff = self._base_backoff
        while True:
            live_status = await self._check_live_status()
            if live_status is False:
                now = datetime.now(timezone.utc)
                if not self._offline_logged:
                    logger.info("TikTok user '@%s' is offline. Will retry.", self.username)
                    self._offline_logged = True
                    self._last_offline_log = now
                try:
                    sleep_for = max(1.0, self._next_live_check_at - time.monotonic())
                    await self._sleep_with_jitter(sleep_for)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    logger.info("Sleep cancelled; shutting down.")
                    break
                continue
            if live_status is None:
                try:
                    sleep_for = max(1.0, self._next_live_check_at - time.monotonic())
                    await self._sleep_with_jitter(sleep_for)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    logger.info("Sleep cancelled; shutting down.")
                    break
                continue
            try:
                await self.client.connect()
                backoff = self._base_backoff
            except WebcastBlocked200Error as exc:
                logger.error(
                    "TikTok rejected the WebSocket (%s). "
                    "Add real TikTok cookies via TIKTOK_SESSIONID or TIKTOK_COOKIES_FILE to avoid DEVICE_BLOCKED.",
                    exc,
                )
                backoff = self._rate_limit_backoff
            except (asyncio.CancelledError, KeyboardInterrupt):
                logger.info("Listener cancelled; shutting down.")
                break
            except Exception as exc:
                msg = str(exc).lower()
                print(msg)
                if (
                    "rate_limit" in msg
                    or "too many connections" in msg
                    or "sign_not_200" in msg
                    or "sign api" in msg
                    or "eulerstream" in msg
                ):
                    now = datetime.now(timezone.utc)
                    if not self._rate_limit_logged or (now - self._last_rate_limit_log) > timedelta(minutes=10):
                        logger.warning(
                            "TikTok rate limit encountered. Backing off for %s seconds.",
                            self._rate_limit_backoff,
                        )
                        self._rate_limit_logged = True
                        self._last_rate_limit_log = now
                    backoff = self._rate_limit_backoff
                if "usernotfound" in msg or "user not found" in msg:
                    now = datetime.now(timezone.utc)
                    if not self._not_found_logged or (now - self._last_not_found_log) > timedelta(hours=1):
                        logger.error("TikTok user '@%s' not found. Check the username.", self.username)
                        self._not_found_logged = True
                        self._last_not_found_log = now
                    backoff = self._not_found_backoff
                if "expecting value" in msg and "line 1 column 1" in msg:
                    now = datetime.now(timezone.utc)
                    if (now - self._last_parse_error_log) > timedelta(minutes=5):
                        logger.warning("TikTok listener received an invalid response. Retrying...")
                        self._last_parse_error_log = now
                    backoff = min(self._max_backoff, max(self._base_backoff, backoff))
                if "offline" in msg or "not live" in msg:
                    if not self._offline_logged:
                        logger.info("TikTok user '@%s' is offline. Will retry.", self.username)
                        self._offline_logged = True
                        self._last_offline_log = datetime.now(timezone.utc)
                    backoff = self._offline_backoff
                logger.error("TikTok listener error: %s", exc)
                if "device_blocked" in msg:
                    backoff = self._rate_limit_backoff
                elif "rate_limit" in msg or "too many connections" in msg:
                    backoff = max(self._rate_limit_backoff, backoff)
            finally:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            try:
                await self._sleep_with_jitter(backoff)
            except (asyncio.CancelledError, KeyboardInterrupt):
                logger.info("Sleep cancelled; shutting down.")
                break
            backoff = min(self._max_backoff, max(self._base_backoff, backoff * 2))

    async def _safe_post(self, url: str, payload: dict) -> bool:
        try:
            await self.http.post(url, json=payload)
            return True
        except Exception as exc:
            logger.warning("HTTP post failed to %s: %s", url, exc)
            return False

    async def close(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:
            pass
        try:
            await self.http.aclose()
        except Exception:
            pass

def parse_usernames() -> list[str]:
    raw = TIKTOK_USERNAMES.strip()
    if raw:
        parts = re.split(r"[,\s]+", raw)
        cleaned = []
        for p in parts:
            val = (p or "").strip().lstrip("@")
            if val:
                cleaned.append(val)
        # de-dupe preserving order
        seen = set()
        out = []
        for v in cleaned:
            if v in seen:
                continue
            seen.add(v)
            out.append(v)
        return out
    return []

async def main() -> None:
    usernames = [u for u in parse_usernames() if u]
    if not usernames:
        logger.error("No TikTok usernames configured. Set TIKTOK_USERNAMES.")
        return
    logger.info("Starting TikTok listeners for: %s", ", ".join([f"@{u}" for u in usernames]))
    supabase_url = f"https://{SUPABASE_PROJECT_ID}.supabase.co"
    if SUPABASE_SECRET_KEY:
        logger.info("Using Supabase secret key for event ingestion.")
    else:
        logger.error("Supabase key missing. Set SUPABASE_SECRET_KEY.")
    if supabase_url:
        key_tail = (SUPABASE_SECRET_KEY or "")[-6:]
        logger.info("Supabase target: %s (key suffix: %s)", supabase_url, key_tail if key_tail else "n/a")
    event_store = SupabaseEventStore(supabase_url, SUPABASE_SECRET_KEY, debug=SUPABASE_DEBUG)
    listeners = [TikTokLiveListener(u, API_BASE, event_store) for u in usernames]

    async def run_listener(listener: TikTokLiveListener) -> None:
        try:
            await listener.run()
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await listener.close()

    try:
        await asyncio.gather(*(run_listener(l) for l in listeners))
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Shutting down TikTok listeners.")
        for l in listeners:
            await l.close()
        await event_store.close()


if __name__ == "__main__":
    asyncio.run(main())

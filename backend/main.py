import logging
import os
import re
import io
import csv
import json
import asyncio
import subprocess
import time
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path
import unicodedata
import uuid
import shutil
from typing import Optional, List, Tuple
from zoneinfo import ZoneInfo

import uvicorn
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import backend.config as config
from backend.obs_controller import OBSController
from backend.state import BattleStateManager
from backend.websocket_manager import WebsocketManager
from scripts.utils import _env

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MEDIA_DIR = (BASE_DIR / ".." / "media").resolve()
DIST_DIR = (BASE_DIR / ".." / "dist").resolve()
MEDIA_DIR.mkdir(exist_ok=True)
HOTKEYS_PATH = MEDIA_DIR / "hotkeys.json"
RPD_QUERIES_PATH = MEDIA_DIR / "rpd_queries.json"
RAPIDAPI_KEY = _env("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = _env("RAPIDAPI_HOST", "")
SUPABASE_PROJECT_ID = _env("SUPABASE_PROJECT_ID", "").strip()
SUPABASE_PUBLISHABLE_KEY = _env("SUPABASE_PUBLISHABLE_KEY", "").strip()
SUPABASE_SECRET_KEY = _env("SUPABASE_SECRET_KEY", "").strip()
SUPABASE_URL = _env("SUPABASE_URL", "").strip()
if not SUPABASE_URL and SUPABASE_PROJECT_ID:
    SUPABASE_URL = f"https://{SUPABASE_PROJECT_ID}.supabase.co"
SUPABASE_CLIENT_KEY = SUPABASE_PUBLISHABLE_KEY
SUPABASE_REST_URL = f"{SUPABASE_URL}/rest/v1" if SUPABASE_URL else ""

ROLE_OPTIONS = {"bell", "duo_sfx", "applause", "mvp", "attention", "background", "win", "closing"}


def _load_hotkeys() -> dict:
    defaults = {
        "fade_in": True,
        "fade_out": True,
        "loop_same_song_after_finish": False,
        "display_background_timer": False,
        "enable_duo_dance": False,
        "hotkeys": {},
    }
    if not HOTKEYS_PATH.exists():
        return defaults
    try:
        payload = json.loads(HOTKEYS_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return defaults
    except Exception:
        return defaults
    fade_in = payload.get("fade_in")
    fade_out = payload.get("fade_out")
    loop_same_song_after_finish = payload.get("loop_same_song_after_finish")
    display_background_timer = payload.get("display_background_timer")
    enable_duo_dance = payload.get("enable_duo_dance")
    hotkeys = payload.get("hotkeys")
    data = {
        "fade_in": True if fade_in is None else bool(fade_in),
        "fade_out": True if fade_out is None else bool(fade_out),
        "loop_same_song_after_finish": bool(loop_same_song_after_finish),
        "display_background_timer": bool(display_background_timer),
        "enable_duo_dance": bool(enable_duo_dance),
        "hotkeys": hotkeys if isinstance(hotkeys, dict) else {},
    }
    return data


def _save_hotkeys(payload: dict) -> None:
    try:
        HOTKEYS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to save hotkeys.json: %s", exc)


def _load_rpd_queries() -> list:
    if not RPD_QUERIES_PATH.exists():
        return []
    try:
        payload = json.loads(RPD_QUERIES_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []
    except Exception:
        return []
    out = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or "").strip()
        expression = str(item.get("expression") or "").strip()
        if not qid or not expression:
            continue
        out.append({
            "id": qid,
            "name": str(item.get("name") or "").strip() or expression,
            "expression": expression,
        })
    return out


def _save_rpd_queries(queries: list) -> None:
    try:
        RPD_QUERIES_PATH.write_text(json.dumps(queries, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to save rpd_queries.json: %s", exc)


def _find_tiktok_id(data, handle: str) -> Optional[str]:
    handle_l = (handle or "").lstrip("@").lower()
    if not handle_l:
        return None
    if isinstance(data, dict):
        unique = data.get("uniqueId") or data.get("unique_id") or data.get("uniqueID")
        if unique and str(unique).lower() == handle_l:
            uid = data.get("id") or data.get("userId") or data.get("user_id")
            if uid:
                return str(uid)
        for val in data.values():
            found = _find_tiktok_id(val, handle_l)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_tiktok_id(item, handle_l)
            if found:
                return found
    return None


def _extract_tiktok_id_from_html(html: str, handle: str) -> Optional[str]:
    if not html:
        return None
    handle_l = (handle or "").lstrip("@").lower()
    if not handle_l:
        return None
    script_ids = ("SIGI_STATE", "__UNIVERSAL_DATA_FOR_REHYDRATION__")
    for script_id in script_ids:
        match = re.search(
            rf'<script[^>]+id="{script_id}"[^>]*>(.*?)</script>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        if not match:
            continue
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        found = _find_tiktok_id(payload, handle_l)
        if found:
            return found
    handle_esc = re.escape(handle_l)
    patterns = [
        rf'"uniqueId":"{handle_esc}".{{0,200}}?"id":"(\d+)"',
        rf'"id":"(\d+)".{{0,200}}?"uniqueId":"{handle_esc}"',
        rf'"userId":"(\d+)".{{0,200}}?"uniqueId":"{handle_esc}"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


async def _lookup_tiktok_id(handle: str) -> Optional[str]:
    handle = (handle or "").strip().lstrip("@")
    if not handle:
        return None
    url = f"https://www.tiktok.com/@{handle}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                return None
            return _extract_tiktok_id_from_html(resp.text, handle)
    except Exception:
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Standalone mode: skip OBS control
    obs.enabled = False
    logger.info("Service started (headless). Overlay websocket at %s", config.WEBSOCKET_PATH)
    try:
        state_manager.rename_media_files(MEDIA_DIR)
    except Exception as exc:
        logger.warning("Media rename on startup failed: %s", exc)
    try:
        state_manager.ensure_song_durations(MEDIA_DIR)
    except Exception as exc:
        logger.warning("Song duration scan failed: %s", exc)
    _cleanup_unregistered_media(state_manager)
    yield


class NoCacheStaticFiles(StaticFiles):
    """Forces browsers to revalidate (cheap 304s) instead of serving a stale cached
    copy of shared JS/HTML across deploys — these files aren't content-hashed, so a
    page can load with a version newer than the JS it depends on (or vice versa)."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app = FastAPI(title="TikTok LIVE Battle Controller", openapi_url="/openapi.json", lifespan=lifespan)
app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")
app.mount("/dist", StaticFiles(directory=str(DIST_DIR), check_dir=False), name="dist")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/config")
async def get_config() -> JSONResponse:
    return JSONResponse({"supabase_url": SUPABASE_URL, "supabase_key": SUPABASE_CLIENT_KEY})

obs = OBSController()
state_manager = BattleStateManager(
    config.OVERLAY_SOURCES,
    library_path=MEDIA_DIR / "songs.json",
    dancers_path=MEDIA_DIR / "dancers.json",
    plays_path=MEDIA_DIR / "play_counts.json",
    points_path=MEDIA_DIR / "points_counts.json",
    analytics_path=MEDIA_DIR / "analytics.sqlite",
)
ws_manager = WebsocketManager()


class BattleStartRequest(BaseModel):
    mode: Optional[str] = None


class ScoreRequest(BaseModel):
    amount: int = 1


class ScoreImportRequest(BaseModel):
    slot_one: int = 0
    slot_two: int = 0


class SlotImportRequest(BaseModel):
    slot_one: Optional[str] = None
    slot_two: Optional[str] = None


class CameraSelectRequest(BaseModel):
    index: Optional[int] = None
    label: Optional[str] = None


class OverlayLayoutRequest(BaseModel):
    x_offset: Optional[float] = 0
    y_offset: Optional[float] = 0
    scale: Optional[float] = 1


class OverlayRotationRequest(BaseModel):
    degrees: Optional[int] = 0


class OverlayFlipRequest(BaseModel):
    flip_x: Optional[bool] = False
    flip_y: Optional[bool] = False


class CameraLikesOverlayRequest(BaseModel):
    like_goal: Optional[str] = ""
    prize: Optional[str] = ""
    target_group_name: Optional[str] = ""
    target_group_handle: Optional[str] = ""


class SongRequest(BaseModel):
    target: str
    url: str
    context: Optional[str] = None


class SongBackgroundRequest(BaseModel):
    url: str


class SongVolumeRequest(BaseModel):
    url: str
    volume: float


class AudioProcessResponse(BaseModel):
    output: str
    note: str = ""
    title: str = ""


class RegisterSongRequest(BaseModel):
    song_id: str
    name: str
    url: str
    artist: Optional[str] = None
    dancers: Optional[List[str]] = None
    front_dancers: Optional[List[str]] = None
    mvp_dancers: Optional[List[str]] = None
    knows_song: Optional[List[str]] = None
    special_dance_for: Optional[List[str]] = None
    roles: Optional[List[str]] = None
    exclusive_mvp_for: Optional[str] = None
    volume: Optional[float] = None
    difficulty: Optional[str] = None
    camera_dance: Optional[bool] = None
    duo_dance: Optional[bool] = None


class TagSongRequest(BaseModel):
    url: str
    dancer: str


class UpdateSongDancersRequest(BaseModel):
    song_id: str
    dancers: List[str]
    front_dancers: List[str]
    mvp_dancers: List[str]
    knows_song: Optional[List[str]] = None
    special_dance_for: Optional[List[str]] = None
    roles: Optional[List[str]] = None
    exclusive_mvp_for: Optional[str] = None
    volume: Optional[float] = None
    difficulty: Optional[str] = None
    camera_dance: Optional[bool] = None
    duo_dance: Optional[bool] = None
    tags: Optional[List[dict]] = None
    battle_disabled: Optional[bool] = None


class RenameSongRequest(BaseModel):
    song_id: str
    name: str
    artist: Optional[str] = None


class DeleteSongRequest(BaseModel):
    song_id: str


class DancerRequest(BaseModel):
    name: str
    handle: str


class WinnerRequest(BaseModel):
    name: str


class GroupRequest(BaseModel):
    name: str


class EnabledDancersRequest(BaseModel):
    names: List[str]


class WinRequest(BaseModel):
    name: str
    wins: Optional[int] = None


class DeleteDancerRequest(BaseModel):
    name: str


class SpoofGiftRequest(BaseModel):
    slot: Optional[str] = None
    competition_id: Optional[str] = None
    amount: Optional[int] = 1


class SpoofStartBattleRequest(BaseModel):
    slot_one: Optional[str] = None
    slot_two: Optional[str] = None
    competition_id: Optional[str] = None
    duration_sec: Optional[int] = 120

class LiveBattleEndRequest(BaseModel):
    start_ms: int
    end_ms: Optional[int] = None
    competition_id: Optional[str] = None


class SettingsRequest(BaseModel):
    fade_in: Optional[bool] = None
    fade_out: Optional[bool] = None
    loop_same_song_after_finish: Optional[bool] = None
    display_background_timer: Optional[bool] = None
    enable_duo_dance: Optional[bool] = None
    hotkeys: Optional[dict] = None


class SaveRpdQueryRequest(BaseModel):
    name: Optional[str] = None
    expression: str


class DeleteRpdQueryRequest(BaseModel):
    id: str


@app.get("/settings/data")
async def get_settings() -> JSONResponse:
    return JSONResponse(_load_hotkeys())


@app.post("/settings/data")
async def update_settings(body: SettingsRequest) -> JSONResponse:
    data = _load_hotkeys()
    if body.fade_in is not None:
        data["fade_in"] = bool(body.fade_in)
    if body.fade_out is not None:
        data["fade_out"] = bool(body.fade_out)
    if body.loop_same_song_after_finish is not None:
        data["loop_same_song_after_finish"] = bool(body.loop_same_song_after_finish)
    if body.display_background_timer is not None:
        data["display_background_timer"] = bool(body.display_background_timer)
    if body.enable_duo_dance is not None:
        data["enable_duo_dance"] = bool(body.enable_duo_dance)
    if body.hotkeys is not None and isinstance(body.hotkeys, dict):
        cleaned = {}
        for key, role in body.hotkeys.items():
            k = str(key)
            if not (len(k) == 1 and k.isdigit()):
                continue
            role_str = str(role).strip() if role is not None else ""
            if not role_str:
                continue
            if role_str not in ROLE_OPTIONS:
                continue
            cleaned[k] = role_str
        data["hotkeys"] = cleaned
    _save_hotkeys(data)
    return JSONResponse(data)


@app.get("/rpd/queries")
async def get_rpd_queries() -> JSONResponse:
    return JSONResponse(_load_rpd_queries())


@app.post("/rpd/queries")
async def save_rpd_query(body: SaveRpdQueryRequest) -> JSONResponse:
    expression = body.expression.strip()
    if not expression:
        return JSONResponse({"error": "expression is required"}, status_code=400)
    queries = _load_rpd_queries()
    name = (body.name or "").strip() or expression
    queries.append({"id": str(uuid.uuid4()), "name": name, "expression": expression})
    _save_rpd_queries(queries)
    return JSONResponse(queries)


@app.post("/rpd/queries/delete")
async def delete_rpd_query(body: DeleteRpdQueryRequest) -> JSONResponse:
    queries = [q for q in _load_rpd_queries() if q["id"] != body.id]
    _save_rpd_queries(queries)
    return JSONResponse(queries)


def _norm_filename(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r'[\\/*?:"<>|]+', "", s)
    s = re.sub(r"\s+", " ", s.strip())
    return s[:200] or "untitled"

def _cleanup_unregistered_media(state_manager: BattleStateManager) -> None:
    """
    Remove mp3 files in media/ that are not referenced by the song library.
    """
    try:
        state = state_manager.get_state()
        lib = state.get("songs", {}).get("library", {}) or {}
        keep = set()
        for val in lib.values():
            url = val.get("url") or ""
            name = url.split("/")[-1] if url else ""
            if name.lower().endswith(".mp3"):
                keep.add(name)
        for path in MEDIA_DIR.glob("*.mp3"):
            if path.name not in keep:
                try:
                    path.unlink()
                    logger.info("Removed unregistered media file: %s", path.name)
                except Exception as exc:
                    logger.warning("Failed to remove %s: %s", path, exc)
    except Exception as exc:
        logger.warning("Cleanup of unregistered media failed: %s", exc)


async def download_via_api(url: str) -> Optional[Path]:
    if not RAPIDAPI_KEY:
        return None
    api_url = f"https://{RAPIDAPI_HOST}/download/mp3"
    headers = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}
    temp_path = None
    try:
        resp = httpx.get(api_url, params={"url": url}, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        link = data.get("downloadUrl") or data.get("link") or data.get("result") or data.get("url")
        if not link:
            return None
        audio = httpx.get(link, timeout=120)
        audio.raise_for_status()
        if not audio.headers.get("content-type", "").startswith("audio/"):
            return None
        temp_path = MEDIA_DIR / f"api_{uuid.uuid4().hex}.mp3"
        temp_path.write_bytes(audio.content)
        return temp_path
    except Exception as exc:
        logger.warning("RapidAPI download failed: %s", exc)
        return None


def _broadcast_state(state: dict) -> None:
    # Fire-and-forget broadcast
    import asyncio

    asyncio.create_task(ws_manager.broadcast({"type": "state", "payload": state}))


def _sync_obs(state: dict) -> None:
    # OBS disabled in headless mode
    return


def _normalize_slot(raw: str) -> str:
    token = raw.lower()
    if token in {"1", "one", "slot1", "slot_one", "a", "alpha"}:
        return "slot_one"
    if token in {"2", "two", "slot2", "slot_two", "b", "beta"}:
        return "slot_two"
    return raw


def _supabase_headers() -> Optional[dict]:
    if not SUPABASE_SECRET_KEY or not SUPABASE_REST_URL:
        return None
    return {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


async def _insert_supabase_row(table: str, row: dict) -> Optional[str]:
    headers = _supabase_headers()
    if not headers:
        return "Supabase credentials missing. Set SUPABASE_URL/SUPABASE_PROJECT_ID and SUPABASE_SECRET_KEY."
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_REST_URL, timeout=10.0) as client:
            resp = await client.post(f"/{table}", json=row, headers=headers)
    except Exception as exc:
        return f"Supabase request failed: {exc}"
    if resp.is_success:
        return None
    try:
        payload = resp.json()
    except Exception:
        payload = resp.text or ""
    return f"Supabase insert failed (status={resp.status_code}): {payload}"


def _resolve_group_handle_from_state(state: dict) -> str:
    group_name = str(state.get("group_name") or "").strip()
    return _resolve_group_handle_by_name(state, group_name)


def _resolve_group_handle_by_name(state: dict, group_name: str) -> str:
    group_name = str(group_name or "").strip()
    if not group_name:
        return ""
    dancers = state.get("dancers") or []
    target = group_name.lower()
    for dancer in dancers:
        name = str(dancer.get("name") or "").strip().lower()
        if name == target:
            handle = str(dancer.get("handle") or "").strip().lstrip("@")
            if handle:
                return handle
    # Fallback: if group_name itself looks like a handle, accept it.
    if re.fullmatch(r"@?[A-Za-z0-9._]{2,64}", group_name):
        return group_name.lstrip("@")
    return ""


def _resolve_group_name_by_handle(state: dict, handle: str) -> str:
    normalized = str(handle or "").strip().lstrip("@").lower()
    if not normalized:
        return ""
    dancers = state.get("dancers") or []
    for dancer in dancers:
        dancer_handle = str(dancer.get("handle") or "").strip().lstrip("@").lower()
        if dancer_handle == normalized:
            return str(dancer.get("name") or "").strip()
    return ""


def _resolve_like_overlay_target_from_state(state: dict) -> tuple[str, str, str]:
    configured_name = str(state.get("like_target_group_name") or "").strip()
    configured_handle = str(state.get("like_target_group_handle") or "").strip().lstrip("@")
    if configured_name or configured_handle:
        name = configured_name
        handle = configured_handle
        if name and not handle:
            handle = _resolve_group_handle_by_name(state, name)
        if handle and not name:
            name = _resolve_group_name_by_handle(state, handle)
        return name, handle, "configured"
    fallback_name = str(state.get("group_name") or "").strip()
    fallback_handle = _resolve_group_handle_from_state(state)
    return fallback_name, fallback_handle, "state.group_name"


def _parse_like_goal_value(raw: str) -> Optional[int]:
    text = str(raw or "").strip()
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None
    try:
        value = int(digits)
    except Exception:
        return None
    return value if value > 0 else None


def _tg_to_int(value) -> Optional[int]:
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


def _gift_row_diamond_amount(row: dict) -> int:
    amount_value = _tg_to_int(row.get("amount_value"))
    if amount_value is not None:
        return max(0, amount_value)
    diamond_count = _tg_to_int(row.get("diamond_count"))
    fan_ticket_count = _tg_to_int(row.get("fan_ticket_count"))
    per_gift = diamond_count if diamond_count is not None else fan_ticket_count
    if per_gift is None:
        return 0
    repeat_count = _tg_to_int(row.get("repeat_count"))
    combo_count = _tg_to_int(row.get("combo_count"))
    multiplier = repeat_count if repeat_count is not None else combo_count
    if multiplier is None:
        multiplier = 1
    return max(0, per_gift * max(1, multiplier))


def _rank_top_gifters(rows: list[dict]) -> dict[str, list[dict]]:
    # Combo gifts emit one row per tick with a cumulative repeat_count and a
    # per-tick amount_value that is NOT simply diamond_count * repeat_count.
    # Collapse all rows sharing the same group_id down to the one with the
    # highest repeat_count, then compute diamonds as repeat_count * diamond_count.
    combo_best: dict[str, dict] = {}
    singles: list[dict] = []
    for row in rows:
        gid = row.get("group_id")
        if gid and str(gid) != "0":
            key = str(gid)
            prev = combo_best.get(key)
            if prev is None or (_tg_to_int(row.get("repeat_count")) or 0) > (_tg_to_int(prev.get("repeat_count")) or 0):
                combo_best[key] = row
        else:
            singles.append(row)

    totals: dict[str, dict[str, dict[str, object]]] = {}
    for row in singles + list(combo_best.values()):
        member = str(row.get("to_member_nickname") or "").strip()
        gifter = str(row.get("from_username") or "").strip()
        nickname = str(row.get("from_nickname") or "").strip()
        if not member:
            continue
        if "the host" in member.lower():
            continue
        if not gifter:
            gifter = "(unknown)"
        gid = row.get("group_id")
        if gid and str(gid) != "0":
            diamond_count = _tg_to_int(row.get("diamond_count")) or 0
            repeat_count = max(1, _tg_to_int(row.get("repeat_count")) or 1)
            diamonds = max(0, diamond_count * repeat_count)
        else:
            diamonds = _gift_row_diamond_amount(row)
        if diamonds <= 0:
            continue
        member_bucket = totals.setdefault(member, {})
        gifter_entry = member_bucket.setdefault(gifter, {"diamonds": 0, "from_nickname": ""})
        gifter_entry["diamonds"] = int(gifter_entry.get("diamonds") or 0) + diamonds
        if nickname:
            gifter_entry["from_nickname"] = nickname

    ranked: dict[str, list[dict]] = {}
    for member, donor_map in totals.items():
        top_items = sorted(
            donor_map.items(),
            key=lambda item: (-int(item[1].get("diamonds") or 0), item[0].lower()),
        )[:5]
        ranked[member] = [
            {
                "from_username": username,
                "from_nickname": str(payload.get("from_nickname") or "").strip(),
                "diamonds": int(payload.get("diamonds") or 0),
            }
            for username, payload in top_items
        ]
    return ranked


async def _fetch_gift_rows_for_window(
    *,
    tiktok_username: str,
    start_utc: datetime,
    end_utc: datetime,
) -> tuple[list[dict], Optional[str]]:
    headers = _supabase_headers()
    if not headers:
        return [], "Supabase credentials missing. Set SUPABASE_URL/SUPABASE_PROJECT_ID and SUPABASE_SECRET_KEY."
    headers = dict(headers)
    headers.pop("Prefer", None)
    page_size = 1000
    max_pages = 250
    offset = 0
    pages = 0
    out: list[dict] = []
    handle = str(tiktok_username or "").strip().lstrip("@")
    if not handle:
        return out, None
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_REST_URL, timeout=12.0) as client:
            while True:
                query = [
                    ("select", "id,iso_ts,to_member_nickname,from_username,from_nickname,amount_value,diamond_count,repeat_count,combo_count,group_id,tiktok_username,gift_name"),
                    ("tiktok_username", f"eq.{handle}"),
                    ("iso_ts", f"gte.{start_utc.astimezone(timezone.utc).isoformat()}"),
                    ("iso_ts", f"lt.{end_utc.astimezone(timezone.utc).isoformat()}"),
                    ("order", "id.asc"),
                ]
                range_headers = dict(headers)
                range_headers["Range-Unit"] = "items"
                range_headers["Range"] = f"{offset}-{offset + page_size - 1}"
                resp = await client.get("/gift_events", params=query, headers=range_headers)
                if not resp.is_success:
                    try:
                        payload = resp.json()
                    except Exception:
                        payload = resp.text or ""
                    return [], f"Supabase gift_events query failed (status={resp.status_code}): {payload}"
                rows = resp.json()
                if not isinstance(rows, list):
                    return [], "Unexpected gift_events response format."
                out.extend([row for row in rows if isinstance(row, dict)])
                if len(rows) < page_size:
                    break
                offset += page_size
                pages += 1
                if pages >= max_pages:
                    return [], "Exceeded gift_events pagination limit."
    except Exception as exc:
        return [], f"Supabase request failed: {exc}"
    return out, None


async def _fetch_latest_total_likes_today(tiktok_username: str) -> tuple[int, Optional[str]]:
    handle = str(tiktok_username or "").strip().lstrip("@")
    if not handle:
        return 0, None
    headers = _supabase_headers()
    if not headers:
        return 0, "Supabase credentials missing. Set SUPABASE_URL/SUPABASE_PROJECT_ID and SUPABASE_SECRET_KEY."
    headers = dict(headers)
    headers.pop("Prefer", None)
    now_utc = datetime.now(timezone.utc)
    start_day = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = start_day + timedelta(days=1)
    start_unix = int(start_day.timestamp())
    end_unix = int(end_day.timestamp())
    params = {
        "select": "total_like_count,unix_ts",
        "tiktok_username": f"ilike.{handle}",
        "total_like_count": "not.is.null",
        "and": f"(unix_ts.gte.{start_unix},unix_ts.lt.{end_unix})",
        "order": "unix_ts.desc",
        "limit": "1",
    }
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_REST_URL, timeout=10.0) as client:
            resp = await client.get("/like_events", headers=headers, params=params)
    except Exception as exc:
        return 0, f"Supabase request failed: {exc}"
    if not resp.is_success:
        try:
            payload = resp.json()
        except Exception:
            payload = resp.text or ""
        return 0, f"Supabase like_events query failed (status={resp.status_code}): {payload}"
    try:
        rows = resp.json()
    except Exception:
        rows = []
    if not isinstance(rows, list) or not rows:
        return 0, None
    total_raw = rows[0].get("total_like_count")
    try:
        total = int(str(total_raw).strip())
    except Exception:
        total = 0
    return max(0, total), None


def _parse_stream_format_jsonl(raw: str) -> tuple[Optional[list], Optional[str]]:
    text = (raw or "").strip()
    if not text:
        return None, "stream_format is required and must be JSONL."
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None, "stream_format is required and must be JSONL."
    rows: list[dict] = []
    for idx, line in enumerate(lines, start=1):
        try:
            parsed = json.loads(line)
        except Exception:
            return None, f"stream_format line {idx} is not valid JSON."
        if not isinstance(parsed, dict):
            return None, f"stream_format line {idx} must be a JSON object."
        segment = str(parsed.get("segment") or "").strip()
        if not segment:
            return None, f"stream_format line {idx} is missing segment."
        duration_raw = parsed.get("duration_min")
        try:
            duration_min = int(str(duration_raw).strip())
        except Exception:
            return None, f"stream_format line {idx} has invalid duration_min."
        if duration_min <= 0:
            return None, f"stream_format line {idx} duration_min must be a positive integer."
        rows.append({"segment": segment, "duration_min": duration_min})
    if not rows:
        return None, "stream_format is empty."
    return rows, None


@app.post("/battle/start")
async def start_battle(body: BattleStartRequest) -> JSONResponse:
    state = state_manager.start_battle(body.mode)
    # OBS scene switching skipped in headless mode
    _sync_obs(state)
    state_manager.set_scene(config.SCENE_BATTLE)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/battle/end")
async def end_battle() -> JSONResponse:
    state = state_manager.end_battle()
    # OBS scene switching skipped in headless mode
    state_manager.set_scene(config.SCENE_MAIN)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/battle/slot/{slot}")
async def set_slot(slot: str, body: SlotImportRequest) -> JSONResponse:
    normalized = _normalize_slot(slot)
    name = body.slot_one if normalized == "slot_one" else body.slot_two
    state = state_manager.assign_slot(normalized, name)
    _sync_obs(state)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/overlay/{name}/show")
async def show_overlay(name: str) -> JSONResponse:
    state = state_manager.set_overlay_state(name, True)
    obs.set_visibility(config.SCENE_BATTLE, name, True)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/overlay/{name}/hide")
async def hide_overlay(name: str) -> JSONResponse:
    state = state_manager.set_overlay_state(name, False)
    obs.set_visibility(config.SCENE_BATTLE, name, False)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/overlay/{name}/layout")
async def set_overlay_layout(name: str, body: OverlayLayoutRequest) -> JSONResponse:
    scale_value = float(body.scale if body.scale is not None else 1.0)
    if scale_value <= 0:
        scale_value = 1.0
    # Keep UI mistakes bounded.
    scale_value = max(0.1, min(8.0, scale_value))
    x_value = float(body.x_offset if body.x_offset is not None else 0.0)
    y_value = float(body.y_offset if body.y_offset is not None else 0.0)
    state = state_manager.set_overlay_layout(name, x_value, y_value, scale_value)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/overlay/rotation")
async def set_overlay_rotation(body: OverlayRotationRequest) -> JSONResponse:
    raw = int(body.degrees if body.degrees is not None else 0)
    normalized = raw % 360
    if normalized not in (0, 90, 180, 270):
        normalized = int(round(normalized / 90.0) * 90) % 360
    state = state_manager.set_overlay_rotation(normalized)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/overlay/flip")
async def set_overlay_flip(body: OverlayFlipRequest) -> JSONResponse:
    state = state_manager.set_overlay_flip(bool(body.flip_x), bool(body.flip_y))
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/score/{slot}/add")
async def increment_score(slot: str, body: ScoreRequest) -> JSONResponse:
    normalized = _normalize_slot(slot)
    state = state_manager.increment_score(normalized, body.amount)
    _sync_obs(state)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/battle/scores/import")
async def import_scores(body: ScoreImportRequest) -> JSONResponse:
    state = state_manager.import_scores(body.slot_one, body.slot_two)
    _sync_obs(state)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/battle/slots/import")
async def import_slots(body: SlotImportRequest) -> JSONResponse:
    state = state_manager.import_slots(body.slot_one, body.slot_two)
    _sync_obs(state)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/battle/winner")
async def set_winner(body: WinnerRequest) -> JSONResponse:
    state = state_manager.set_last_winner(body.name)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/battle/group")
async def set_group(body: GroupRequest) -> JSONResponse:
    state = state_manager.set_group_name(body.name)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/dancers/enabled")
async def set_enabled_dancers(body: EnabledDancersRequest) -> JSONResponse:
    state = state_manager.set_enabled_dancers(body.names)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/battle/win")
async def register_win(body: WinRequest) -> JSONResponse:
    if body.wins is not None:
        state = state_manager.set_wins_for(body.name, body.wins)
    else:
        state = state_manager.increment_win(body.name)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/battle/spoof_gift")
async def spoof_gift(body: SpoofGiftRequest = SpoofGiftRequest()) -> JSONResponse:
    state = state_manager.get_state()
    slot = (body.slot or "slot_one").strip().lower()
    slot = _normalize_slot(slot)
    slot_name = (state.get(slot) or "").strip()
    if not slot_name:
        return JSONResponse({"ok": False, "error": f"{slot.replace('_', ' ').title()} is not set."}, status_code=400)
    dancers = state.get("dancers", []) or []
    match = next((d for d in dancers if (d.get("name") or "").lower() == slot_name.lower()), None)
    if not match:
        return JSONResponse({"ok": False, "error": f"{slot.replace('_', ' ').title()} dancer not found."}, status_code=400)
    slot_one_name = (state.get("slot_one") or "").strip()
    slot_two_name = (state.get("slot_two") or "").strip()
    match_one = next((d for d in dancers if (d.get("name") or "").lower() == slot_one_name.lower()), None)
    match_two = next((d for d in dancers if (d.get("name") or "").lower() == slot_two_name.lower()), None)
    if not match_one or not match_two:
        return JSONResponse({"ok": False, "error": "Both Dancer 1 and Dancer 2 must be set."}, status_code=400)

    team_1_id = str(match_one.get("tiktok_id") or match_one.get("tiktokId") or "").strip()
    team_2_id = str(match_two.get("tiktok_id") or match_two.get("tiktokId") or "").strip()
    if not team_1_id or not team_2_id:
        return JSONResponse({"ok": False, "error": "Dancer 1 and Dancer 2 must both have TikTok IDs."}, status_code=400)

    amount = int(body.amount or 1)
    if amount <= 0:
        amount = 1

    scores = state.get("scores", {}) or {}
    score_one = int(scores.get("slot_one") or 0)
    score_two = int(scores.get("slot_two") or 0)
    if slot == "slot_one":
        score_one += amount
    else:
        score_two += amount

    comp_id_raw = str(body.competition_id or "").strip()
    if comp_id_raw and comp_id_raw.isdigit():
        competition_id = int(comp_id_raw)
    else:
        competition_id = int(time.time() * 1000)

    group_name = (state.get("group_name") or "").strip().lower()
    group_match = next((d for d in dancers if (d.get("name") or "").strip().lower() == group_name), None)
    tiktok_username = str((group_match or {}).get("handle") or "DUMMY").strip().lstrip("@") or "DUMMY"

    now_dt = datetime.now(timezone.utc)
    row_id = int(time.time() * 1000) * 1000 + random.randint(0, 999)
    raw_row = {
        "id": row_id,
        "event_type": "CompetitionScoreUpdate",
        "iso_ts": now_dt.isoformat(),
        "unix_ts": int(now_dt.timestamp()),
        "payload": "DUMMY",
        "msgId": row_id,
        "tiktok_username": tiktok_username,
    }
    raw_err = await _insert_supabase_row("tiktok_events_raw", raw_row)
    if raw_err and "23505" not in raw_err and "duplicate key" not in raw_err:
        logger.warning("Spoof raw insert failed: %s", raw_err)
        return JSONResponse({"ok": False, "error": raw_err}, status_code=500)
    row = {
        "id": row_id,
        "iso_ts": now_dt.isoformat(),
        "unix_ts": int(now_dt.timestamp()),
        "room_id": 0,
        "create_time_ms": int(now_dt.timestamp() * 1000),
        "message_id": row_id,
        "competition_id": competition_id,
        "competition_room_id": 0,
        "competition_type": "BATTLE_TYPE_GROUP_SHOW",
        "competition_message_type": "COMPETITION_MESSAGE_TYPE_SCORE_CHANGE",
        "active_stage": "score_change",
        "team_count": 2,
        "team_1_score": score_one,
        "team_2_score": score_two,
        "team_1_member_ids": [team_1_id],
        "team_2_member_ids": [team_2_id],
        "total_score": score_one + score_two,
        "team_infos_json": json.dumps(
            [
                {"teamId": team_1_id, "score": score_one, "users": [{"userId": team_1_id, "nickname": slot_one_name}]},
                {"teamId": team_2_id, "score": score_two, "users": [{"userId": team_2_id, "nickname": slot_two_name}]},
            ],
            ensure_ascii=False,
        ),
        "tiktok_username": tiktok_username,
    }
    err = await _insert_supabase_row("competition_events", row)
    if err:
        logger.warning("Spoof competition score insert failed: %s", err)
        return JSONResponse({"ok": False, "error": err}, status_code=500)
    return JSONResponse({"ok": True, "competition_id": competition_id, "slot": slot, "team_1_score": score_one, "team_2_score": score_two})


@app.post("/battle/spoof_start_battle")
async def spoof_start_battle(body: SpoofStartBattleRequest = SpoofStartBattleRequest()) -> JSONResponse:
    state = state_manager.get_state()
    dancers = state.get("dancers", []) or []

    slot_one_name = (body.slot_one or state.get("slot_one") or "").strip()
    slot_two_name = (body.slot_two or state.get("slot_two") or "").strip()
    if not slot_one_name or not slot_two_name:
        return JSONResponse({"ok": False, "error": "Dancer 1 and Dancer 2 must both be selected."}, status_code=400)

    dancer_one = next((d for d in dancers if (d.get("name") or "").lower() == slot_one_name.lower()), None)
    dancer_two = next((d for d in dancers if (d.get("name") or "").lower() == slot_two_name.lower()), None)
    if not dancer_one or not dancer_two:
        return JSONResponse({"ok": False, "error": "Selected dancers not found."}, status_code=400)

    team_1_id = str(dancer_one.get("tiktok_id") or dancer_one.get("tiktokId") or "").strip()
    team_2_id = str(dancer_two.get("tiktok_id") or dancer_two.get("tiktokId") or "").strip()
    if not team_1_id or not team_2_id:
        return JSONResponse({"ok": False, "error": "Selected dancers must both have TikTok IDs."}, status_code=400)

    comp_id_raw = str(body.competition_id or "").strip()
    if comp_id_raw and comp_id_raw.isdigit():
        competition_id = int(comp_id_raw)
    else:
        competition_id = int(time.time() * 1000)

    duration_sec = int(body.duration_sec or 120)
    if duration_sec < 1:
        duration_sec = 120

    group_name = (state.get("group_name") or "").strip().lower()
    group_match = next((d for d in dancers if (d.get("name") or "").strip().lower() == group_name), None)
    tiktok_username = str((group_match or {}).get("handle") or "DUMMY").strip().lstrip("@") or "DUMMY"

    now_dt = datetime.now(timezone.utc)
    now_sec = int(now_dt.timestamp())
    end_ts = now_sec + duration_sec
    row_id = int(time.time() * 1000) * 1000 + random.randint(0, 999)
    raw_row = {
        "id": row_id,
        "event_type": "startCompetition",
        "iso_ts": now_dt.isoformat(),
        "unix_ts": now_sec,
        "payload": "DUMMY",
        "msgId": row_id,
        "tiktok_username": tiktok_username,
    }
    raw_err = await _insert_supabase_row("tiktok_events_raw", raw_row)
    if raw_err and "23505" not in raw_err and "duplicate key" not in raw_err:
        logger.warning("Spoof start raw insert failed: %s", raw_err)
        return JSONResponse({"ok": False, "error": raw_err}, status_code=500)

    team_infos = [
        {"teamId": team_1_id, "score": 0, "users": [{"userId": team_1_id, "nickname": slot_one_name}]},
        {"teamId": team_2_id, "score": 0, "users": [{"userId": team_2_id, "nickname": slot_two_name}]},
    ]
    row = {
        "id": row_id,
        "iso_ts": now_dt.isoformat(),
        "unix_ts": now_sec,
        "message_id": row_id,
        "room_id": 0,
        "create_time_ms": int(now_dt.timestamp() * 1000),
        "competition_id": competition_id,
        "competition_room_id": 0,
        "competition_type": "BATTLE_TYPE_GROUP_SHOW",
        "competition_message_type": "COMPETITION_MESSAGE_TYPE_START",
        "active_stage": "start",
        "team_count": 2,
        "team_1_score": 0,
        "team_2_score": 0,
        "team_1_member_ids": [team_1_id],
        "team_2_member_ids": [team_2_id],
        "end_timestamp": end_ts,
        "end_timestamp_actual": end_ts,
        "total_score": 0,
        "team_infos_json": json.dumps(team_infos, ensure_ascii=False),
        "tiktok_username": tiktok_username,
    }

    table_name = "competition_events"
    err = await _insert_supabase_row(table_name, row)
    if err:
        logger.warning("Spoof start insert failed (%s): %s", table_name, err)
        return JSONResponse({"ok": False, "error": err}, status_code=500)

    return JSONResponse(
        {
            "ok": True,
            "competition_id": competition_id,
            "slot_one": slot_one_name,
            "slot_two": slot_two_name,
            "end_timestamp": end_ts,
            "inserted_table": table_name,
        }
    )


@app.post("/battle/battle_end")
async def live_battle_end(body: LiveBattleEndRequest) -> JSONResponse:
    state = state_manager.get_state()
    scores = state.get("scores", {}) or {}
    slot_one_name = (state.get("slot_one") or "").strip()
    slot_two_name = (state.get("slot_two") or "").strip()
    dancer_list = state.get("dancers", []) or []

    def _find_dancer(name: str) -> dict:
        target = (name or "").lower()
        for d in dancer_list:
            if (d.get("name") or "").lower() == target:
                return d
        return {}

    dancer_one = _find_dancer(slot_one_name)
    dancer_two = _find_dancer(slot_two_name)
    score_one = int(scores.get("slot_one") or 0)
    score_two = int(scores.get("slot_two") or 0)
    if score_one == score_two:
        winner = "tie"
    elif score_one > score_two:
        winner = slot_one_name or "slot_one"
    else:
        winner = slot_two_name or "slot_two"

    start_ms = int(body.start_ms)
    end_ms = int(body.end_ms) if body.end_ms is not None else int(time.time() * 1000)
    start_dt = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    end_dt = datetime.fromtimestamp(end_ms / 1000, timezone.utc)

    row_id = int(time.time() * 1000) * 1000 + random.randint(0, 999)
    competition_id = str(body.competition_id or "").strip()
    if not competition_id:
        competition_id = f"DUMMY-{row_id}"
    row = {
        "id": row_id,
        "competition_id": competition_id,
        "slot_one_name": slot_one_name,
        "slot_two_name": slot_two_name,
        "slot_one_handle": dancer_one.get("handle"),
        "slot_two_handle": dancer_two.get("handle"),
        "slot_one_tiktok_id": dancer_one.get("tiktok_id") or dancer_one.get("tiktokId"),
        "slot_two_tiktok_id": dancer_two.get("tiktok_id") or dancer_two.get("tiktokId"),
        "slot_one_points": score_one,
        "slot_two_points": score_two,
        "winner": winner,
        "start_iso_ts": start_dt.isoformat(),
        "start_unix_ts": int(start_dt.timestamp()),
        "end_iso_ts": end_dt.isoformat(),
        "end_unix_ts": int(end_dt.timestamp()),
    }
    err = await _insert_supabase_row("battle_results", row)
    if err:
        logger.warning("Battle event insert failed: %s", err)
        return JSONResponse({"ok": False, "error": err}, status_code=500)
    return JSONResponse({"ok": True})


@app.post("/camera/select")
async def select_camera(body: CameraSelectRequest) -> JSONResponse:
    logger.debug("Camera select request: index=%s label=%s", body.index, body.label)
    if body.index is not None:
        state_manager.set_camera_index(body.index)
    if body.label is not None:
        state_manager.set_camera_label(body.label)
    state = state_manager.get_state()
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/songs/play")
async def play_song(body: SongRequest) -> JSONResponse:
    state = state_manager.set_current_song(body.target, body.url)
    # increment play count for this url
    state = state_manager.increment_play(body.url, context=body.context or "")
    _broadcast_state(state)
    return JSONResponse(state)


def _find_ffmpeg() -> str:
    env = os.environ.get("FFMPEG_BIN", "").strip()
    if env:
        return env
    repo_root = Path(__file__).resolve().parent.parent
    candidates = list(repo_root.glob("scripts/ffmpeg-bin/**/ffmpeg.exe"))
    if candidates:
        return str(candidates[0])
    return "ffmpeg"


def _dshow_video_devices() -> list[str]:
    ffmpeg = _find_ffmpeg()
    try:
        result = subprocess.run(
            [ffmpeg, "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        logger.debug("FFmpeg device probe failed (%s): %s", ffmpeg, exc)
        return []
    output = (result.stderr or "") + "\n" + (result.stdout or "")
    devices: list[str] = []
    for line in output.splitlines():
        if "Alternative name" in line:
            continue
        match = re.search(r'"([^"]+)"\s*\(video\)', line)
        if match:
            devices.append(match.group(1))
    return devices


@app.get("/camera/devices")
async def camera_devices() -> JSONResponse:
    devices = _dshow_video_devices()
    return JSONResponse({"devices": [{"index": i, "label": name} for i, name in enumerate(devices)]})


@app.post("/songs/background")
async def set_background_song(body: SongBackgroundRequest) -> JSONResponse:
    state = state_manager.set_song("background", body.url)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/songs/volume")
async def set_song_volume(body: SongVolumeRequest) -> JSONResponse:
    state = state_manager.set_song_volume(body.url, body.volume)
    _broadcast_state(state)
    return JSONResponse(state)


@app.get("/songs")
async def get_songs() -> JSONResponse:
    return JSONResponse(state_manager.get_state().get("songs", {}))


@app.post("/songs/register")
async def register_song(body: RegisterSongRequest) -> JSONResponse:
    state = state_manager.register_song(
        song_id=body.song_id,
        name=body.name,
        url=body.url,
        dancers=body.dancers,
        front_dancers=body.front_dancers,
        mvp_dancers=body.mvp_dancers,
        roles=body.roles,
        knows_song=body.knows_song,
        special_dance_for=body.special_dance_for,
        exclusive_mvp_for=body.exclusive_mvp_for,
        volume=body.volume,
        difficulty=body.difficulty,
        camera_dance=body.camera_dance,
        duo_dance=body.duo_dance,
        artist=body.artist,
    )
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/songs/rename")
async def rename_song(body: RenameSongRequest) -> JSONResponse:
    state = state_manager.rename_song(body.song_id, body.name, body.artist)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/songs/tag")
async def tag_song(body: TagSongRequest) -> JSONResponse:
    ignored = {"group", "slot_one", "slot_two", "slot one", "slot two"}
    if body.dancer.strip().lower() in ignored:
        return JSONResponse(state_manager.get_state())
    state = state_manager.tag_song(body.url, body.dancer)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/dancers/register")
async def register_dancer(body: DancerRequest) -> JSONResponse:
    tiktok_id = await _lookup_tiktok_id(body.handle)
    state = state_manager.add_dancer(body.name, body.handle, tiktok_id)
    _broadcast_state(state)
    return JSONResponse(state)

@app.post("/dancers/refresh_ids")
async def refresh_dancer_ids() -> JSONResponse:
    dancers = state_manager.get_state().get("dancers", []) or []
    updates = {}
    for dancer in dancers:
        handle = (dancer.get("handle") or "").strip()
        if not handle:
            continue
        tiktok_id = await _lookup_tiktok_id(handle)
        if tiktok_id:
            updates[handle.lstrip("@").lower()] = tiktok_id
        await asyncio.sleep(0.35)
    state = state_manager.update_dancer_ids(updates)
    _broadcast_state(state)
    return JSONResponse(state)

@app.post("/dancers/delete")
async def delete_dancer(body: DeleteDancerRequest) -> JSONResponse:
    state = state_manager.delete_dancer(body.name)
    _broadcast_state(state)
    return JSONResponse(state)


@app.get("/dancers")
async def get_dancers() -> JSONResponse:
    return JSONResponse(state_manager.get_state().get("dancers", []))


@app.post("/songs/update_dancers")
async def update_song_dancers(body: UpdateSongDancersRequest) -> JSONResponse:
    state = state_manager.update_song_dancers(
        body.song_id,
        body.dancers,
        body.front_dancers,
        body.mvp_dancers,
        body.roles,
        body.knows_song,
        body.special_dance_for,
        body.exclusive_mvp_for,
        body.volume,
        body.difficulty,
        body.camera_dance,
        body.duo_dance,
        body.tags,
        body.battle_disabled,
    )
    _broadcast_state(state)
    return JSONResponse(state)


@app.get("/analytics/data")
async def analytics_data() -> JSONResponse:
    state = state_manager.get_state()
    lib = state.get("songs", {}).get("library", {}) or {}
    stats = state_manager.get_analytics_stats() or {}
    items = []
    for url, vals in stats.items():
        name = url
        for meta in lib.values():
            if meta.get("url") == url:
                name = meta.get("name") or url
                break
        items.append(
            {
                "url": url,
                "name": name,
                "count": int(vals.get("play_count", 0)),
                "points": int(vals.get("points_count", 0)),
            }
        )
    items.sort(key=lambda x: x["points"], reverse=True)
    return JSONResponse({"plays": items})


class PointsRequest(BaseModel):
    url: Optional[str] = None
    amount: int = 1


class StreamNoteRequest(BaseModel):
    host: str = ""
    stream_date: str = ""
    group: str = ""
    outfit_theme: str = ""
    mvp: str = ""
    members_present: List[str] = []
    stream_format: str = ""
    additional_notes: str = ""


@app.post("/analytics/points")
async def add_points(req: PointsRequest) -> JSONResponse:
    # if url not provided, use current song
    target_url = req.url
    if not target_url:
        target_url = state_manager.get_state().get("songs", {}).get("current", "")
    state = state_manager.increment_points(target_url or "", req.amount)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/camera/likes/settings")
async def set_camera_likes_overlay_settings(body: CameraLikesOverlayRequest) -> JSONResponse:
    current_state = state_manager.get_state()
    target_group_name = str(body.target_group_name or "").strip()
    target_group_handle = str(body.target_group_handle or "").strip().lstrip("@")
    if target_group_name and not target_group_handle:
        target_group_handle = _resolve_group_handle_by_name(current_state, target_group_name)
    if target_group_handle and not target_group_name:
        target_group_name = _resolve_group_name_by_handle(current_state, target_group_handle)
    state = state_manager.set_like_overlay(
        body.like_goal,
        body.prize,
        target_group_name,
        target_group_handle,
    )
    _broadcast_state(state)
    return JSONResponse(state)


@app.get("/camera/likes/summary")
async def camera_likes_summary() -> JSONResponse:
    state = state_manager.get_state()
    group_name, tiktok_username, target_source = _resolve_like_overlay_target_from_state(state)
    like_goal_text = str(state.get("like_goal") or "").strip()
    prize_text = str(state.get("prize") or "").strip()
    goal_value = _parse_like_goal_value(like_goal_text)
    total_likes = 0
    source = "unavailable"
    warning: Optional[str] = None

    if tiktok_username:
        total_likes, err = await _fetch_latest_total_likes_today(tiktok_username)
        if err:
            warning = err
            source = "error"
        else:
            source = "like_events"
    else:
        source = "no-group-handle"

    progress = None
    goal_reached = False
    if goal_value is not None and goal_value > 0:
        progress = min(1.0, float(total_likes) / float(goal_value))
        goal_reached = total_likes >= goal_value

    payload = {
        "group_name": group_name,
        "tiktok_username": tiktok_username,
        "total_likes": max(0, int(total_likes)),
        "like_goal": like_goal_text,
        "goal_value": goal_value,
        "progress": progress,
        "goal_reached": goal_reached,
        "prize": prize_text,
        "source": source,
        "target_source": target_source,
        "target_group_name": str(state.get("like_target_group_name") or "").strip(),
        "target_group_handle": str(state.get("like_target_group_handle") or "").strip().lstrip("@"),
    }
    if warning:
        payload["warning"] = warning
    return JSONResponse(payload)


@app.get("/top-gifters/summary")
async def top_gifters_summary(group_handle: str = "", date_mt: Optional[str] = None) -> JSONResponse:
    handle = str(group_handle or "").strip().lstrip("@")
    if not handle:
        return JSONResponse({"ok": False, "error": "group_handle is required."}, status_code=400)
    try:
        mt_tz = ZoneInfo("America/Denver")
    except Exception:
        mt_tz = timezone.utc

    if date_mt:
        try:
            day = datetime.strptime(str(date_mt).strip(), "%Y-%m-%d").date()
        except Exception:
            return JSONResponse({"ok": False, "error": "date_mt must be YYYY-MM-DD."}, status_code=400)
    else:
        day = datetime.now(mt_tz).date()

    start_local = datetime.combine(day, datetime.min.time(), tzinfo=mt_tz)
    end_local = start_local + timedelta(days=1)
    rows, err = await _fetch_gift_rows_for_window(
        tiktok_username=handle,
        start_utc=start_local.astimezone(timezone.utc),
        end_utc=end_local.astimezone(timezone.utc),
    )
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=500)
    ranked = _rank_top_gifters(rows)
    members = []
    for member in sorted(ranked.keys(), key=lambda x: x.lower()):
        rankings = ranked[member]
        members.append(
            {
                "member": member,
                "gifters": [
                    {
                        "rank": idx,
                        "username": str(g.get("from_username") or ""),
                        "from_nickname": str(g.get("from_nickname") or ""),
                        "diamonds": int(g.get("diamonds") or 0),
                        "usd": round(int(g.get("diamonds") or 0) * 0.005, 2),
                    }
                    for idx, g in enumerate(rankings, start=1)
                ],
            }
        )
    return JSONResponse(
        {
            "ok": True,
            "group_handle": handle,
            "date_mt": day.isoformat(),
            "start_utc": start_local.astimezone(timezone.utc).isoformat(),
            "end_utc": end_local.astimezone(timezone.utc).isoformat(),
            "rows_count": len(rows),
            "members_count": len(members),
            "members": members,
        }
    )


@app.post("/notes/stream")
async def add_stream_note(body: StreamNoteRequest) -> JSONResponse:
    now_dt = datetime.now(timezone.utc)
    iso_ts = now_dt.isoformat()
    unix_ts = int(now_dt.timestamp())
    stream_date_raw = (body.stream_date or "").strip()
    if stream_date_raw:
        try:
            stream_date_value = datetime.strptime(stream_date_raw, "%Y-%m-%d").date().isoformat()
        except Exception:
            return JSONResponse({"ok": False, "error": "stream_date must be in YYYY-MM-DD format."}, status_code=400)
    else:
        stream_date_value = now_dt.date().isoformat()
    stream_format_rows, stream_format_err = _parse_stream_format_jsonl(body.stream_format)
    if stream_format_err:
        return JSONResponse({"ok": False, "error": stream_format_err}, status_code=400)

    members = []
    for item in body.members_present or []:
        name = (item or "").strip()
        if not name:
            continue
        if name in members:
            continue
        members.append(name)

    row = {
        "iso_ts": iso_ts,
        "unix_ts": unix_ts,
        "stream_date": stream_date_value,
        "host": (body.host or "").strip(),
        "group_name": (body.group or "").strip(),
        "outfit_theme": (body.outfit_theme or "").strip(),
        "mvp": (body.mvp or "").strip(),
        "members_present": members,
        "stream_format": stream_format_rows,
        "additional_notes": (body.additional_notes or "").strip(),
    }
    err = await _insert_supabase_row("stream_notes", row)
    if err:
        logger.warning("Stream note insert failed: %s", err)
        return JSONResponse({"ok": False, "error": err}, status_code=500)
    return JSONResponse({"ok": True, "row": row})


@app.get("/notes/stream/history")
async def stream_notes_history(limit: int = 10, offset: int = 0) -> JSONResponse:
    page_size = max(1, min(100, int(limit)))
    page_offset = max(0, int(offset))
    headers = _supabase_headers()
    if not headers:
        return JSONResponse(
            {
                "ok": False,
                "error": "Supabase credentials missing. Set SUPABASE_URL/SUPABASE_PROJECT_ID and SUPABASE_SECRET_KEY.",
            },
            status_code=500,
        )
    headers = dict(headers)
    headers.pop("Prefer", None)
    params = [
        (
            "select",
            "iso_ts,unix_ts,stream_date,host,group_name,outfit_theme,mvp,members_present,stream_format,additional_notes",
        ),
        ("order", "unix_ts.desc,iso_ts.desc"),
        ("limit", str(page_size + 1)),
        ("offset", str(page_offset)),
    ]
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_REST_URL, timeout=12.0) as client:
            resp = await client.get("/stream_notes", params=params, headers=headers)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Supabase request failed: {exc}"}, status_code=500)
    if not resp.is_success:
        try:
            payload = resp.json()
        except Exception:
            payload = resp.text or ""
        return JSONResponse(
            {"ok": False, "error": f"Supabase stream_notes query failed (status={resp.status_code}): {payload}"},
            status_code=500,
        )
    try:
        rows = resp.json()
    except Exception:
        rows = []
    if not isinstance(rows, list):
        return JSONResponse({"ok": False, "error": "Unexpected stream_notes response format."}, status_code=500)
    notes = [row for row in rows if isinstance(row, dict)]
    has_more = len(notes) > page_size
    if has_more:
        notes = notes[:page_size]
    return JSONResponse(
        {
            "ok": True,
            "notes": notes,
            "limit": page_size,
            "offset": page_offset,
            "next_offset": page_offset + len(notes),
            "has_more": has_more,
        }
    )


@app.get("/analytics")
async def analytics_page() -> FileResponse:
    return _static_file("analytics.html")


@app.get("/settings")
async def settings_page() -> FileResponse:
    return _static_file("settings.html")


@app.get("/notes")
async def notes_page() -> FileResponse:
    return _static_file("notes.html")


@app.get("/notes/attendance")
async def notes_attendance_page() -> FileResponse:
    return _static_file("attendance.html")


@app.get("/top/gifters")
async def top_gifters_page() -> FileResponse:
    return _static_file("top_gifters.html")


@app.get("/gifts/list")
async def gifts_list(group_handle: str = "", date_mt: Optional[str] = None) -> JSONResponse:
    handle = str(group_handle or "").strip().lstrip("@")
    if not handle:
        return JSONResponse({"ok": False, "error": "group_handle is required."}, status_code=400)
    try:
        mt_tz = ZoneInfo("America/Denver")
    except Exception:
        mt_tz = timezone.utc

    if date_mt:
        try:
            day = datetime.strptime(str(date_mt).strip(), "%Y-%m-%d").date()
        except Exception:
            return JSONResponse({"ok": False, "error": "date_mt must be YYYY-MM-DD."}, status_code=400)
    else:
        day = datetime.now(mt_tz).date()

    start_local = datetime.combine(day, datetime.min.time(), tzinfo=mt_tz)
    end_local = start_local + timedelta(days=1)
    rows, err = await _fetch_gift_rows_for_window(
        tiktok_username=handle,
        start_utc=start_local.astimezone(timezone.utc),
        end_utc=end_local.astimezone(timezone.utc),
    )
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=500)

    combo_best: dict[str, dict] = {}
    singles: list[dict] = []
    for row in rows:
        gid = row.get("group_id")
        if gid and str(gid) != "0":
            key = str(gid)
            prev = combo_best.get(key)
            if prev is None or (_tg_to_int(row.get("repeat_count")) or 0) > (_tg_to_int(prev.get("repeat_count")) or 0):
                combo_best[key] = row
        else:
            singles.append(row)

    gifts = []
    for row in singles + list(combo_best.values()):
        gid = row.get("group_id")
        if gid and str(gid) != "0":
            diamond_count = _tg_to_int(row.get("diamond_count")) or 0
            repeat_count = max(1, _tg_to_int(row.get("repeat_count")) or 1)
            diamonds = max(0, diamond_count * repeat_count)
        else:
            diamonds = _gift_row_diamond_amount(row)
        if diamonds <= 0:
            continue
        recipient = str(row.get("to_member_nickname") or "").strip()
        if not recipient or "the host" in recipient.lower():
            recipient = ""
        gift_name = str(row.get("gift_name") or "").strip() or "Unknown"
        gifts.append({
            "id": str(row.get("id") or ""),
            "iso_ts": str(row.get("iso_ts") or ""),
            "from_username": str(row.get("from_username") or "").strip(),
            "from_nickname": str(row.get("from_nickname") or "").strip(),
            "gift_name": gift_name,
            "diamonds": diamonds,
            "usd": round(diamonds * 0.005, 2),
            "recipient": recipient,
        })

    gifts.sort(key=lambda g: str(g.get("iso_ts") or ""))
    gift_types = sorted(set(g["gift_name"] for g in gifts if g["gift_name"] and g["gift_name"] != "Unknown"))

    return JSONResponse({
        "ok": True,
        "group_handle": handle,
        "date_mt": day.isoformat(),
        "total_diamonds": sum(g["diamonds"] for g in gifts),
        "gifts": gifts,
        "gift_types": gift_types,
    })


@app.get("/gifts")
async def gifts_page() -> FileResponse:
    return _static_file("gifts.html")


@app.get("/analytics/plays")
async def analytics_plays(limit: int = 200, offset: int = 0) -> JSONResponse:
    items = state_manager.get_play_events(limit=limit, offset=offset)
    return JSONResponse({"events": items})


@app.get("/analytics/plays.csv")
async def analytics_plays_csv(limit: int = 1000, offset: int = 0) -> PlainTextResponse:
    rows = state_manager.get_play_events_csv(limit=limit, offset=offset)
    output = io.StringIO()
    writer = csv.writer(output)
    for row in rows:
        writer.writerow(row)
    return PlainTextResponse(output.getvalue(), media_type="text/csv")


@app.post("/songs/scan_durations")
async def scan_song_durations() -> JSONResponse:
    try:
        state_manager.ensure_song_durations(MEDIA_DIR)
    except Exception as exc:
        logger.warning("Song duration scan failed: %s", exc)
    state = state_manager.get_state()
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/songs/delete")
async def delete_song(body: DeleteSongRequest) -> JSONResponse:
    state = state_manager.delete_song(body.song_id)
    _broadcast_state(state)
    return JSONResponse(state)


@app.get("/state")
async def get_state() -> JSONResponse:
    return JSONResponse(state_manager.get_state())


@app.websocket(config.WEBSOCKET_PATH)
async def websocket_endpoint(websocket: WebSocket) -> None:
    await ws_manager.connect(websocket)
    try:
        await websocket.send_json({"type": "state", "payload": state_manager.get_state()})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)


def _static_file(filename: str) -> FileResponse:
    path = STATIC_DIR / filename
    return FileResponse(str(path))


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    path = STATIC_DIR / "favicon.ico"
    return FileResponse(str(path))


@app.get("/battle/dances")
async def control_panel() -> FileResponse:
    return _static_file("battle_dances.html")


@app.get("/overlay")
async def overlay_page() -> FileResponse:
    return _static_file("overlay.html")


@app.get("/audio/tools")
async def audio_tools_page() -> FileResponse:
    return _static_file("audio_tools.html")


@app.get("/camera/manage")
async def camera_page() -> FileResponse:
    return _static_file("camera.html")


@app.get("/songs/edit")
async def songs_edit_page() -> FileResponse:
    return _static_file("song_editor.html")


@app.get("/dancers/register")
async def dancers_page() -> FileResponse:
    return _static_file("dancers.html")


@app.get("/group/dances")
async def group_dances_page() -> FileResponse:
    return _static_file("group_dances.html")


@app.get("/dances/menu")
async def dances_menu_page() -> FileResponse:
    return _static_file("solo_dances.html")


@app.get("/dances/solo")
async def dances_solo_page() -> FileResponse:
    return _static_file("solo_dances.html")


@app.get("/dances/rpd")
async def dances_rpd_page() -> FileResponse:
    return _static_file("rpd.html")


@app.get("/app")
async def spa_root() -> FileResponse:
    return _static_file("app.html")


@app.get("/")
async def root_page() -> FileResponse:
    return _static_file("app.html")


@app.post("/audio/process", response_model=AudioProcessResponse)
async def process_audio(
    source_url: Optional[str] = Form(None),
    start: Optional[float] = Form(None),
    end: Optional[float] = Form(None),
    file: Optional[UploadFile] = File(None),
) -> JSONResponse:
    """
    Accepts a YouTube/URL or uploaded mp4/mp3, trims to start/end if ffmpeg is available,
    saves to media/, and returns local path for use in the player.
    """
    import shutil
    import subprocess
    import uuid

    out_name = f"{uuid.uuid4().hex}.mp3"
    out_path = MEDIA_DIR / out_name
    input_path = None
    note = ""
    title = ""

    async def fetch_title(url: str) -> str:
        try:
            resp = httpx.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("title", "") or ""
        except Exception:
            return ""
        return ""

    if file:
        temp_path = MEDIA_DIR / f"upload_{uuid.uuid4().hex}_{file.filename}"
        with temp_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        input_path = temp_path
        title = file.filename
    elif source_url:
        title = await fetch_title(source_url) or title
        input_path = await download_via_api(source_url)
        if input_path is None:
            try:
                r = httpx.get(source_url, timeout=20)
                r.raise_for_status()
                ctype = r.headers.get("content-type", "")
                if not (ctype.startswith("audio/") or ctype.startswith("video/")):
                    raise ValueError(f"Content-Type not audio/video ({ctype})")
                suffix = ".mp3" if ctype.startswith("audio/") else ".mp4"
                temp_path = MEDIA_DIR / f"download_{uuid.uuid4().hex}{suffix}"
                temp_path.write_bytes(r.content)
                input_path = temp_path
            except Exception as exc2:
                return JSONResponse(
                    status_code=400,
                    content={
                        "output": "",
                        "note": f"Download failed via RapidAPI and direct fetch: {exc2}. Upload a file or set RAPIDAPI_KEY."
                    },
                )
    else:
        return JSONResponse(status_code=400, content={"output": "", "note": "No source provided"})

    # If no trimming requested and file is already mp3, just return
    if (start is None and end is None) and str(input_path).lower().endswith(".mp3"):
        return JSONResponse({"output": str(input_path).replace(str(BASE_DIR.parent), "").replace("\\", "/"), "note": "Downloaded", "title": title})

    ffmpeg_bin = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
    if ffmpeg_bin and not Path(ffmpeg_bin).exists():
        ffmpeg_bin = None
    if not ffmpeg_bin:
        return JSONResponse(
            status_code=400,
            content={"output": "", "note": "ffmpeg not available. Set FFMPEG_BIN to a valid ffmpeg.exe path or install ffmpeg on PATH."},
        )

    cmd = [ffmpeg_bin, "-y"]
    if start is not None:
        cmd += ["-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    cmd += ["-i", str(input_path), "-vn", "-acodec", "libmp3lame", "-b:a", "192k", str(out_path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        note = "Processed with ffmpeg"
    except subprocess.CalledProcessError as exc:
        return JSONResponse(
            status_code=500, content={"output": "", "note": f"ffmpeg failed: {exc.stderr.decode(errors='ignore')}"}
        )

    return JSONResponse({"output": f"/media/{out_name}", "note": note or "Downloaded", "title": title})

EXCLUDE_TERMS = {"tutorial", "dancetutorial", "slow"}


def _find_short_url(query: str) -> Optional[str]:
    try:
        import yt_dlp
    except ImportError:
        logger.warning("yt_dlp not installed for short search")
        return None
    def _looks_like_short(entry: dict) -> bool:
        dur = entry.get("duration")
        return isinstance(dur, (int, float)) and dur > 0 and dur <= 90
    def _is_excluded(entry: dict) -> bool:
        fields = " ".join(str(entry.get(k, "")) for k in ("title", "description", "tags")).lower()
        return any(term in fields for term in EXCLUDE_TERMS)
    # Get duration data; avoid flat extraction so duration is available
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": False,
        "default_search": "ytsearch",
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch12:{query}", download=False)
        entries = (info or {}).get("entries") or []
        shorts = []
        for e in entries:
            if _is_excluded(e):
                logger.debug("Excluded entry for '%s' due to terms: %s", query, e.get("title"))
                continue
            if _looks_like_short(e):
                shorts.append(e)
        if not shorts:
            logger.warning("No shorts found for query '%s'", query)
            return None
        shorts.sort(key=lambda e: e.get("duration") or 9999)
        target = shorts[0]
        return target.get("url") or target.get("webpage_url")
    except Exception as exc:
        logger.warning("Short search failed for '%s': %s", query, exc)
    return None


def _download_short_mp3(url: str, title: str, artist: str) -> Optional[Tuple[str, str]]:
    """Returns (public_path, note) or None"""
    try:
        import yt_dlp
    except ImportError:
        logger.warning("yt_dlp not installed for short download")
        return None
    out_name = f"{_norm_filename(artist)} - {_norm_filename(title)}.mp3"
    out_path = MEDIA_DIR / out_name
    ydl_opts = {
        "quiet": True,
        "format": "bestaudio/best",
        "outtmpl": str(MEDIA_DIR / f"tmp_%(id)s.%(ext)s"),
        "noplaylist": True,
    }
    temp_file = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if not info:
            logger.warning("yt_dlp returned no info for %s", url)
            return None
        for r in (info.get("requested_downloads") or []):
            if r.get("filepath"):
                temp_file = r["filepath"]
                break
        if not temp_file:
            vid = info.get("id")
            ext = info.get("ext") or "m4a"
            guess = MEDIA_DIR / f"tmp_{vid}.{ext}"
            if guess.exists():
                temp_file = str(guess)
        if not temp_file:
            logger.warning("No temp file produced for %s", url)
            return None
        ffmpeg_bin = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
        if ffmpeg_bin and Path(ffmpeg_bin).exists():
            cmd = [ffmpeg_bin, "-y", "-i", temp_file, "-vn", "-acodec", "libmp3lame", "-b:a", "192k", str(out_path)]
            subprocess.run(cmd, check=True, capture_output=True)
        else:
            shutil.copyfile(temp_file, out_path)
        return (f"/media/{out_name}", "Downloaded short")
    except Exception as exc:
        logger.warning("Short download failed for %s: %s", url, exc)
        return None
    finally:
        try:
            if temp_file and os.path.exists(temp_file):
                os.remove(temp_file)
        except Exception:
            pass


class CSVBatchRequest(BaseModel):
    csv_text: str


@app.post("/audio/batch_csv")
async def audio_batch_csv(body: CSVBatchRequest) -> JSONResponse:
    """
    Accepts CSV text with columns: title, artist
    Returns per-row status and output path if downloaded.
    """
    text = body.csv_text or ""
    reader = csv.reader(io.StringIO(text))
    results = []
    latest_state = None
    for row in reader:
        if not row or len(row) < 2:
            continue
        title, artist = row[0].strip(), row[1].strip()
        if not title and not artist:
            continue
        if title.lower() == "title" and artist.lower() == "artist":
            continue
        query = f"{artist} {title}".strip()
        url = _find_short_url(query)
        if not url:
            results.append({"title": title, "artist": artist, "status": "error", "note": "no short found"})
            continue
        dl_tmp = await download_via_api(url)
        if not dl_tmp:
            results.append({"title": title, "artist": artist, "status": "error", "note": "download failed"})
            continue
        final_name = f"{_norm_filename(artist)} - {_norm_filename(title)}.mp3"
        final_path = MEDIA_DIR / final_name
        try:
            shutil.move(str(dl_tmp), final_path)
        except Exception:
            final_path = dl_tmp
        public = f"/media/{final_path.name}"
        song_id = uuid.uuid4().hex
        latest_state = state_manager.register_song(
            song_id=song_id,
            name=title or final_path.stem,
            url=public,
            dancers=[],
            front_dancers=[],
            mvp_dancers=[],
            artist=artist,
        )
        results.append({"title": title, "artist": artist, "status": "ok", "output": public, "note": "Downloaded via API", "song_id": song_id})
    if latest_state:
        _broadcast_state(latest_state)
    return JSONResponse({"results": results})


def run() -> None:
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    run()

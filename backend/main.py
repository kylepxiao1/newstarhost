import logging
import os
import re
import io
import csv
import unicodedata
import uuid
import shutil
from pathlib import Path
from typing import Optional, List, Tuple

import uvicorn
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import backend.config as config
from backend.obs_controller import OBSController
from backend.state import BattleStateManager
from backend.websocket_manager import WebsocketManager

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MEDIA_DIR = (BASE_DIR / ".." / "media").resolve()
MEDIA_DIR.mkdir(exist_ok=True)
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "522756c8bamshcbed3268bd8d8a7p15af50jsn5acd33380e60")
RAPIDAPI_HOST = os.environ.get("RAPIDAPI_HOST", "youtube-mp310.p.rapidapi.com")


@asynccontextmanager
async def lifespan(app: FastAPI):
    obs.connect()
    try:
        obs.ensure_capture_source_visible(config.SCENE_BATTLE, config.CACHE_CAMERA_SOURCE)
        obs.ensure_capture_source_visible(config.SCENE_MAIN, config.CACHE_CAMERA_SOURCE)
    except Exception:
        logger.debug("Camera source ensure skipped")
    logger.info("Service started. Overlay websocket at %s", config.WEBSOCKET_PATH)
    _cleanup_unregistered_media(state_manager)
    yield
    obs.disconnect()


app = FastAPI(title="TikTok LIVE Battle Controller", openapi_url="/openapi.json", lifespan=lifespan)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

obs = OBSController()
state_manager = BattleStateManager(
    config.OVERLAY_SOURCES,
    library_path=MEDIA_DIR / "songs.json",
    dancers_path=MEDIA_DIR / "dancers.json",
)
ws_manager = WebsocketManager()


class BattleStartRequest(BaseModel):
    mode: Optional[str] = None


class ScoreRequest(BaseModel):
    amount: int = 1


class SlotImportRequest(BaseModel):
    slot_one: Optional[str] = None
    slot_two: Optional[str] = None


class CameraSelectRequest(BaseModel):
    index: Optional[int] = None
    label: Optional[str] = None


class SongRequest(BaseModel):
    target: str
    url: str


class SongBackgroundRequest(BaseModel):
    url: str


class AudioProcessResponse(BaseModel):
    output: str
    note: str = ""
    title: str = ""


class RegisterSongRequest(BaseModel):
    song_id: str
    name: str
    url: str
    dancers: Optional[List[str]] = None
    front_dancers: Optional[List[str]] = None
    mvp_dancers: Optional[List[str]] = None
    knows_song: Optional[List[str]] = None
    roles: Optional[List[str]] = None


class TagSongRequest(BaseModel):
    url: str
    dancer: str


class UpdateSongDancersRequest(BaseModel):
    song_id: str
    dancers: List[str]
    front_dancers: List[str]
    mvp_dancers: List[str]
    knows_song: Optional[List[str]] = None
    roles: Optional[List[str]] = None


class RenameSongRequest(BaseModel):
    song_id: str
    name: str


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
    slot_one_name = state.get("slot_one") or ""
    slot_two_name = state.get("slot_two") or ""
    scores = state.get("scores") or {"slot_one": 0, "slot_two": 0}
    obs.refresh_scoreboard(slot_one_name, slot_two_name, scores.get("slot_one", 0), scores.get("slot_two", 0))


def _normalize_slot(raw: str) -> str:
    token = raw.lower()
    if token in {"1", "one", "slot1", "slot_one", "a", "alpha"}:
        return "slot_one"
    if token in {"2", "two", "slot2", "slot_two", "b", "beta"}:
        return "slot_two"
    return raw


@app.post("/battle/start")
async def start_battle(body: BattleStartRequest) -> JSONResponse:
    state = state_manager.start_battle(body.mode)
    obs.set_scene(config.SCENE_BATTLE)
    _sync_obs(state)
    state_manager.set_scene(config.SCENE_BATTLE)
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/battle/end")
async def end_battle() -> JSONResponse:
    state = state_manager.end_battle()
    obs.set_scene(config.SCENE_MAIN)
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


@app.post("/score/{slot}/add")
async def increment_score(slot: str, body: ScoreRequest) -> JSONResponse:
    normalized = _normalize_slot(slot)
    state = state_manager.increment_score(normalized, body.amount)
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


@app.post("/camera/select")
async def select_camera(body: CameraSelectRequest) -> JSONResponse:
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
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/songs/background")
async def set_background_song(body: SongBackgroundRequest) -> JSONResponse:
    state = state_manager.set_song("background", body.url)
    _broadcast_state(state)
    return JSONResponse(state)


@app.get("/songs")
async def get_songs() -> JSONResponse:
    return JSONResponse(state_manager.get_state().get("songs", {}))


@app.post("/songs/register")
async def register_song(body: RegisterSongRequest) -> JSONResponse:
    state = state_manager.register_song(
        body.song_id,
        body.name,
        body.url,
        body.dancers,
        body.front_dancers,
        body.mvp_dancers,
        body.roles,
        body.knows_song,
    )
    _broadcast_state(state)
    return JSONResponse(state)


@app.post("/songs/rename")
async def rename_song(body: RenameSongRequest) -> JSONResponse:
    state = state_manager.rename_song(body.song_id, body.name)
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
    state = state_manager.add_dancer(body.name, body.handle)
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
    state = state_manager.update_song_dancers(body.song_id, body.dancers, body.front_dancers, body.mvp_dancers, body.roles, body.knows_song)
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


@app.get("/battle/dances")
async def control_panel() -> FileResponse:
    return _static_file("battle_dances.html")


@app.get("/overlay")
async def overlay_page() -> FileResponse:
    return _static_file("overlay.html")


@app.get("/audio/tools")
async def audio_tools_page() -> FileResponse:
    return _static_file("audio_tools.html")


@app.get("/songs/edit")
async def songs_edit_page() -> FileResponse:
    return _static_file("song_editor.html")


@app.get("/dancers/register")
async def dancers_page() -> FileResponse:
    return _static_file("dancers.html")


@app.get("/group/dances")
async def group_dances_page() -> FileResponse:
    return _static_file("intro_dances.html")


@app.get("/dances/menu")
async def dances_menu_page() -> FileResponse:
    return _static_file("menu_dances.html")


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
        latest_state = state_manager.register_song(song_id, title or final_path.stem, public, [], [], [], None, None)
        results.append({"title": title, "artist": artist, "status": "ok", "output": public, "note": "Downloaded via API", "song_id": song_id})
    if latest_state:
        _broadcast_state(latest_state)
    return JSONResponse({"results": results})


def run() -> None:
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    run()

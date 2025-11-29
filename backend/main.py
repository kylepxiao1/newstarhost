import logging
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import backend.config as config
from backend.obs_controller import OBSController
from backend.state import BattleStateManager
from backend.websocket_manager import WebsocketManager

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="TikTok LIVE Battle Controller", openapi_url="/openapi.json")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

obs = OBSController()
state_manager = BattleStateManager(config.OVERLAY_SOURCES)
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


@app.on_event("startup")
async def startup_event() -> None:
    obs.connect()
    try:
        obs.ensure_capture_source_visible(config.SCENE_BATTLE, config.CACHE_CAMERA_SOURCE)
        obs.ensure_capture_source_visible(config.SCENE_MAIN, config.CACHE_CAMERA_SOURCE)
    except Exception:
        logger.debug("Camera source ensure skipped")
    logger.info("Service started. Overlay websocket at %s", config.WEBSOCKET_PATH)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    obs.disconnect()


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


@app.post("/camera/select")
async def select_camera(body: CameraSelectRequest) -> JSONResponse:
    if body.index is not None:
        state_manager.set_camera_index(body.index)
    if body.label is not None:
        state_manager.set_camera_label(body.label)
    state = state_manager.get_state()
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


@app.get("/battle/control")
async def control_panel() -> FileResponse:
    return _static_file("control.html")


@app.get("/overlay")
async def overlay_page() -> FileResponse:
    return _static_file("overlay.html")


def run() -> None:
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    run()

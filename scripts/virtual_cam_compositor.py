import asyncio
import json
import logging
import os
from typing import Dict

import cv2
import httpx
import numpy as np
import pyvirtualcam
import websockets
from pyvirtualcam import PixelFormat

API_BASE = os.environ.get("BATTLE_API", "http://127.0.0.1:8000")
DEFAULT_CAM_INDEX = int(os.environ.get("INPUT_CAM_INDEX", -1))  # -1 => auto-pick first working camera
WIDTH = int(os.environ.get("CAM_WIDTH", 1280))
HEIGHT = int(os.environ.get("CAM_HEIGHT", 720))
FPS = int(os.environ.get("CAM_FPS", 30))
POLL_INTERVAL = float(os.environ.get("STATE_POLL_SECS", 5.0))
WS_PATH = os.environ.get("STATE_WS_PATH", "/ws/state")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("virtual-cam")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# Silence verbose OpenCV backend selection chatter (handle older OpenCVs)
try:
    if hasattr(cv2, "setLogLevel"):
        level = getattr(cv2, "LOG_LEVEL_ERROR", None)
        if level is None and hasattr(cv2, "utils") and hasattr(cv2.utils, "logging"):
            level = getattr(cv2.utils.logging, "LOG_LEVEL_ERROR", None)
        if level is None and hasattr(cv2, "ERROR"):
            level = cv2.ERROR  # fallback constant name
        if level is None:
            level = 3  # default error level
        cv2.setLogLevel(level)
    elif hasattr(cv2, "utils") and hasattr(cv2.utils, "logging"):
        cv2.utils.logging.setLogLevel(getattr(cv2.utils.logging, "LOG_LEVEL_ERROR", 3))
except Exception:
    pass


async def fetch_state(client: httpx.AsyncClient) -> Dict:
    try:
        resp = await client.get(f"{API_BASE}/state", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch state: %s", exc)
        return {}


async def ws_state_listener(state_holder: Dict):
    ws_url = API_BASE.replace("http", "ws") + WS_PATH
    while True:
        try:
            async with websockets.connect(ws_url) as websocket:
                async for msg in websocket:
                    try:
                        data = json.loads(msg)
                        if data.get("type") == "state":
                            state_holder["state"] = data["payload"]
                    except Exception:
                        continue
        except Exception as exc:
            logger.debug("WebSocket state listener retrying: %s", exc)
            await asyncio.sleep(2)


def draw_overlay(frame: np.ndarray, state: Dict) -> np.ndarray:
    """
    Render overlays with resolution-aware sizing so text stays crisp at any resolution.
    """
    overlay = frame.copy()
    wins = state.get("win_counts") or {}
    enabled = set((state.get("enabled_dancers") or []))
    dancers = state.get("dancers") or []
    display_dancers = dancers if not enabled else [d for d in dancers if (d.get("name") or "") in enabled]
    overlays = state.get("overlay_states") or {"CenterDottedLine": True, "BurstOverlay": True, "BattleScore": True}
    font = cv2.FONT_HERSHEY_SIMPLEX

    # Scale elements based on frame height (smaller text)
    base_h = max(1, frame.shape[0])
    scale = max(0.7, (base_h / 720.0) * 0.9)
    thick = max(1, int(scale * 2))
    line_step = max(20, int(base_h / 38))

    if overlays.get("CenterDottedLine", True):
        center_x = frame.shape[1] // 2
        dash = max(18, int(line_step * 0.9))   # longer dashes
        gap = max(14, int(line_step * 0.7))   # larger gaps
        white_thick = max(3, int(scale * 3.2))  # thicker
        black_thick = max(2, int(scale * 1.8))
        y = 0
        while y < frame.shape[0]:
            y2 = min(y + dash, frame.shape[0])
            cv2.line(overlay, (center_x, y), (center_x, y2), (255, 255, 255), white_thick)
            cv2.line(overlay, (center_x, y), (center_x, y2), (0, 0, 0), black_thick)
            y += dash + gap

    if overlays.get("BurstOverlay", True):
        mask = np.zeros_like(frame)
        rad = int(min(frame.shape[0], frame.shape[1]) * 0.18)
        cv2.circle(mask, (int(frame.shape[1] * 0.25), int(frame.shape[0] * 0.25)), rad, (0, 128, 255), -1)
        cv2.circle(mask, (int(frame.shape[1] * 0.75), int(frame.shape[0] * 0.75)), rad, (255, 64, 128), -1)
        overlay = cv2.addWeighted(overlay, 0.9, mask, 0.1, 0)

    if overlays.get("BattleScore", True):
        def outlined_text(img, text, org):
            cv2.putText(img, text, org, font, scale, (0, 0, 0), thick + 1, cv2.LINE_AA)
            cv2.putText(img, text, org, font, scale, (255, 255, 255), thick, cv2.LINE_AA)

        y = int(50 * scale)
        step = int(36 * scale)
        for dancer in display_dancers:
            name = dancer.get("name") or "Waiting"
            outlined_text(overlay, f"{name}: {wins.get(name, 0)} wins", (40, y))
            y += step
    return overlay


def open_cam(idx: int, label: str = "") -> cv2.VideoCapture:
    backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
    if label:
        for backend in backends:
            cap = cv2.VideoCapture(f"video={label}", backend)
            if cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    logger.info("Opened camera by label '%s' via backend %s", label, backend)
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
                    cap.set(cv2.CAP_PROP_FPS, FPS)
                    return cap
                cap.release()
    for backend in backends:
        cap = cv2.VideoCapture(idx, backend)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
            cap.set(cv2.CAP_PROP_FPS, FPS)
            ret, frame = cap.read()
            if ret and frame is not None:
                logger.info("Opened camera index %s via backend %s", idx, backend)
                return cap
            cap.release()
    return cv2.VideoCapture()


async def main() -> None:
    current_idx = DEFAULT_CAM_INDEX
    current_label = ""
    cap = open_cam(current_idx if current_idx >= 0 else 0)
    if current_idx < 0 or not cap.isOpened():
        cap.release()
        chosen = None
        for i in range(0, 10):
            test = open_cam(i)
            ret, frame = test.read()
            if ret and frame is not None:
                chosen = test
                current_idx = i
                logger.info("Auto-selected camera index %s", i)
                break
            test.release()
        if chosen is None:
            raise RuntimeError("Cannot open any camera (indexes 0-9)")
        cap = chosen
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open camera index {current_idx}")

    fail_count = 0

    async with httpx.AsyncClient() as client:
        state_holder: Dict = {"state": await fetch_state(client)}
        ws_task = asyncio.create_task(ws_state_listener(state_holder))
        with pyvirtualcam.Camera(width=WIDTH, height=HEIGHT, fps=FPS, fmt=PixelFormat.BGR) as cam:
            logger.info("Virtual camera started: %s", cam.device)
            while True:
                desired_idx = state_holder.get("state", {}).get("camera_index", -1)
                desired_label = state_holder.get("state", {}).get("camera_label", "")
                if (desired_idx != -1 and desired_idx != current_idx) or (desired_label and desired_label != current_label):
                    logger.info("Switching camera to index %s label '%s'", desired_idx, desired_label)
                    cap.release()
                    new_cap = open_cam(desired_idx if desired_idx != -1 else current_idx, desired_label)
                    if new_cap.isOpened():
                        cap = new_cap
                        current_idx = desired_idx if desired_idx != -1 else current_idx
                        current_label = desired_label
                    else:
                        logger.warning("Failed to open camera index %s label '%s'; keeping previous", desired_idx, desired_label)

                ret, frame = cap.read()
                if not ret or frame is None:
                    logger.warning("Camera frame grab failed")
                    fail_count += 1
                    if fail_count > 30:
                        logger.warning("Reopening camera after repeated failures")
                        cap.release()
                        cap = open_cam(current_idx if current_idx >= 0 else 0)
                        fail_count = 0
                        await asyncio.sleep(0.1)
                        continue
                    await asyncio.sleep(0.01)
                    continue
                fail_count = 0
                if frame.shape[0] != HEIGHT or frame.shape[1] != WIDTH:
                    frame = cv2.resize(frame, (WIDTH, HEIGHT))
                overlayed = draw_overlay(frame, state_holder.get("state") or {})
                cam.send(overlayed)
                cam.sleep_until_next_frame()

                try:
                    state_task = asyncio.create_task(fetch_state(client))
                    await asyncio.wait_for(asyncio.shield(state_task), timeout=POLL_INTERVAL)
                    state_holder["state"] = state_task.result() or state_holder.get("state") or {}
                except asyncio.TimeoutError:
                    pass


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict

import cv2
import httpx
import numpy as np
import pyvirtualcam
import websockets
from pyvirtualcam import PixelFormat

API_BASE = os.environ.get("API_BASE", "https://newstarhost.fly.dev") # http://127.0.0.1:8000
DEFAULT_CAM_INDEX = int(os.environ.get("INPUT_CAM_INDEX", -1))  # -1 => auto-pick first working camera
DEFAULT_WIDTH = int(os.environ.get("CAM_WIDTH", 1280))
DEFAULT_HEIGHT = int(os.environ.get("CAM_HEIGHT", 720))
FPS = int(os.environ.get("CAM_FPS", 30))
POLL_INTERVAL = float(os.environ.get("STATE_POLL_SECS", 5.0))
WS_PATH = os.environ.get("STATE_WS_PATH", "/ws/state")
CAM_OPEN_RETRIES = int(os.environ.get("CAM_OPEN_RETRIES", 4))
CAM_OPEN_DELAY = float(os.environ.get("CAM_OPEN_DELAY", 0.35))
CAM_PROBE_MAX_RES = os.environ.get("CAM_PROBE_MAX_RES", "1").strip().lower() not in {"0", "false", "no", "off"}
CAM_MAX_RES_CANDIDATES = os.environ.get(
    "CAM_MAX_RES_CANDIDATES",
    # include common landscape + portrait camera modes
    "1080x1920,1920x1080,1200x1600,1600x1200,720x1280,1280x720,540x960,960x540,480x854,854x480,768x1024,1024x768,600x800,800x600,480x640,640x480",
).strip()
CAM_PROBE_MAX_CANDIDATES = max(1, int(os.environ.get("CAM_PROBE_MAX_CANDIDATES", "6")))
CAM_PROBE_BUDGET_SECS = max(0.0, float(os.environ.get("CAM_PROBE_BUDGET_SECS", "0.5")))
CAM_PROBE_SETTLE_SECS = max(0.0, float(os.environ.get("CAM_PROBE_SETTLE_SECS", "0.02")))
CAM_VERBOSE_LOGS = os.environ.get("CAM_VERBOSE_LOGS", "0").strip().lower() in {"1", "true", "yes", "on"}

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


def _parse_resolution_candidates(text: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for part in text.split(","):
        token = part.strip().lower()
        if not token or "x" not in token:
            continue
        w_raw, h_raw = token.split("x", 1)
        try:
            w = int(w_raw.strip())
            h = int(h_raw.strip())
        except Exception:
            continue
        if w > 0 and h > 0:
            out.append((w, h))
    # remove duplicates
    dedup: list[tuple[int, int]] = []
    seen = set()
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        dedup.append(item)
    # order by resolution (largest area first), then portrait over landscape
    # for equal area: portrait (h>w), square (h==w), landscape (w>h)
    def _sort_key(item: tuple[int, int]) -> tuple[int, int, int, int]:
        w, h = item
        area = w * h
        if h > w:
            orientation_rank = 0
        elif h == w:
            orientation_rank = 1
        else:
            orientation_rank = 2
        return (-area, orientation_rank, -max(w, h), -min(w, h))

    dedup.sort(key=_sort_key)
    return dedup


RESOLUTION_CANDIDATES = _parse_resolution_candidates(CAM_MAX_RES_CANDIDATES)


def _verbose_log(msg: str, *args) -> None:
    if CAM_VERBOSE_LOGS:
        logger.info(msg, *args)


def _resolution_candidates_for_label(label: str = "") -> list[tuple[int, int]]:
    if not RESOLUTION_CANDIDATES:
        return []
    # Candidates are already sorted globally by area, then portrait preference.
    return RESOLUTION_CANDIDATES


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


def _set_capture_defaults(cap: cv2.VideoCapture) -> None:
    try:
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        cap.set(cv2.CAP_PROP_FOURCC, fourcc)
    except Exception:
        pass
    try:
        cap.set(cv2.CAP_PROP_FPS, FPS)
    except Exception:
        pass


def _probe_best_resolution(
    cap: cv2.VideoCapture,
    label: str = "",
    current_size: tuple[int, int] | None = None,
) -> tuple[int, int] | None:
    candidates = _resolution_candidates_for_label(label)
    if not CAM_PROBE_MAX_RES or not candidates:
        return None
    candidates = candidates[:CAM_PROBE_MAX_CANDIDATES]
    best_w, best_h = (current_size or (0, 0))
    best_area = best_w * best_h
    started = time.monotonic()
    for w, h in candidates:
        if CAM_PROBE_BUDGET_SECS > 0 and (time.monotonic() - started) >= CAM_PROBE_BUDGET_SECS:
            break
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(w))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(h))
            if CAM_PROBE_SETTLE_SECS > 0:
                time.sleep(CAM_PROBE_SETTLE_SECS)
            rw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            rh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            if rw <= 0 or rh <= 0:
                continue
            area = rw * rh
            if area > best_area:
                best_w, best_h = rw, rh
                best_area = area
            # Candidates are sorted high->low. Once backend reports close to requested,
            # take it immediately to keep camera switches responsive.
            if rw >= int(w * 0.95) and rh >= int(h * 0.95):
                best_w, best_h = rw, rh
                best_area = area
                break
        except Exception:
            continue
    if best_w > 0 and best_h > 0:
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(best_w))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(best_h))
        except Exception:
            pass
        return best_w, best_h
    return None


def open_cam(idx: int, label: str = "") -> cv2.VideoCapture:
    backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
    _verbose_log("Attempting camera open idx=%s label='%s'", idx, label)
    if label:
        _verbose_log("Ignoring label '%s' (index-only selection enabled)", label)
    for backend in backends:
        for attempt in range(1, CAM_OPEN_RETRIES + 1):
            _verbose_log(
                "Trying index %s via backend %s (attempt %s/%s)",
                idx,
                backend,
                attempt,
                CAM_OPEN_RETRIES,
            )
            cap = cv2.VideoCapture(idx, backend)
            if cap.isOpened():
                _set_capture_defaults(cap)
                ret, frame = cap.read()
                if ret and frame is not None:
                    actual_w, actual_h = _capture_size(cap, frame)
                    best = _probe_best_resolution(cap, label, (actual_w, actual_h))
                    if best and best != (actual_w, actual_h):
                        ret2, frame2 = cap.read()
                        if ret2 and frame2 is not None:
                            frame = frame2
                            actual_w, actual_h = _capture_size(cap, frame2)
                    logger.info(
                        "Opened camera index %s via backend %s (attempt %s/%s) at %sx%s%s",
                        idx,
                        backend,
                        attempt,
                        CAM_OPEN_RETRIES,
                        actual_w,
                        actual_h,
                        " [max-probed]" if best else "",
                    )
                    return cap
                cap.release()
            else:
                cap.release()
            if attempt < CAM_OPEN_RETRIES:
                time.sleep(CAM_OPEN_DELAY)
    return cv2.VideoCapture()


def _capture_size(cap: cv2.VideoCapture, frame: np.ndarray | None = None) -> tuple[int, int]:
    if frame is not None and getattr(frame, "shape", None) is not None and len(frame.shape) >= 2:
        h, w = int(frame.shape[0]), int(frame.shape[1])
        if w > 0 and h > 0:
            return w, h
    try:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    return max(1, DEFAULT_WIDTH), max(1, DEFAULT_HEIGHT)


def _restart_virtual_camera(
    cam: pyvirtualcam.Camera | None,
    requested_width: int,
    requested_height: int,
    current_width: int,
    current_height: int,
) -> tuple[pyvirtualcam.Camera, int, int]:
    def _size_candidates(w: int, h: int) -> list[tuple[int, int]]:
        w = max(1, int(w))
        h = max(1, int(h))
        out: list[tuple[int, int]] = []

        def _add(cw: int, ch: int) -> None:
            cw = max(1, int(cw))
            ch = max(1, int(ch))
            item = (cw, ch)
            if item not in out:
                out.append(item)

        _add(w, h)
        # Keep aspect ratio while downscaling for backends that reject large modes.
        for factor in (0.9, 0.8, 0.75, 2 / 3, 0.6, 0.5, 0.4, 1 / 3, 0.25):
            _add(round(w * factor), round(h * factor))

        landscape_common = [(1920, 1080), (1600, 900), (1280, 720), (960, 540), (854, 480), (640, 360)]
        portrait_common = [(1080, 1920), (900, 1600), (720, 1280), (540, 960), (480, 854), (360, 640)]
        if h > w:
            for cw, ch in portrait_common + landscape_common:
                _add(cw, ch)
        else:
            for cw, ch in landscape_common + portrait_common:
                _add(cw, ch)
        return out

    candidates = _size_candidates(requested_width, requested_height)
    errors: list[str] = []
    for cand_w, cand_h in candidates:
        try:
            new_cam = pyvirtualcam.Camera(width=cand_w, height=cand_h, fps=FPS, fmt=PixelFormat.BGR)
            if cam is not None:
                try:
                    cam.close()
                except Exception:
                    pass
            if cand_w == requested_width and cand_h == requested_height:
                logger.info("Virtual camera started: %s (%sx%s @ %sfps)", new_cam.device, cand_w, cand_h, FPS)
            else:
                logger.warning(
                    "Virtual camera fallback: requested %sx%s, using %sx%s",
                    requested_width,
                    requested_height,
                    cand_w,
                    cand_h,
                )
                logger.info("Virtual camera started: %s (%sx%s @ %sfps)", new_cam.device, cand_w, cand_h, FPS)
            return new_cam, cand_w, cand_h
        except Exception as exc:
            errors.append(f"{cand_w}x{cand_h}: {exc}")
            continue

    if cam is not None:
        error_count = len(errors)
        latest_error = errors[-1] if errors else "unknown"
        logger.warning(
            "Virtual camera restart failed for requested %sx%s; keeping existing %sx%s (%s attempted sizes). Last error: %s",
            requested_width,
            requested_height,
            current_width,
            current_height,
            error_count,
            latest_error,
        )
        _verbose_log(
            "Virtual camera restart detailed errors: %s",
            "; ".join(errors[-3:]) if errors else "unknown",
        )
        return cam, current_width, current_height
    raise RuntimeError(
        f"Could not start virtual camera for requested {requested_width}x{requested_height}. "
        f"Recent errors: {'; '.join(errors[-3:]) if errors else 'unknown'}"
    )


def _find_ffmpeg() -> str:
    env = os.environ.get("FFMPEG_BIN", "").strip()
    if env:
        return env
    repo_root = Path(__file__).resolve().parent.parent
    candidates = list(repo_root.glob("scripts/ffmpeg-bin/**/ffmpeg.exe"))
    if candidates:
        return str(candidates[0])
    return "ffmpeg"


def log_dshow_devices() -> None:
    ffmpeg = _find_ffmpeg()
    try:
        result = subprocess.run(
            [ffmpeg, "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        logger.info("FFmpeg device probe failed (%s): %s", ffmpeg, exc)
        return
    output = (result.stderr or "") + "\n" + (result.stdout or "")
    devices = []
    for line in output.splitlines():
        if "Alternative name" in line:
            continue
        match = re.search(r'"([^"]+)"\s*\(video\)', line)
        if match:
            devices.append(match.group(1))
    if devices:
        logger.info("DirectShow video devices (ffmpeg order): %s", list(enumerate(devices)))
    else:
        logger.info("DirectShow device probe returned no video devices")


async def main() -> None:
    log_dshow_devices()
    last_open_attempt = 0.0

    async with httpx.AsyncClient() as client:
        state_holder: Dict = {"state": await fetch_state(client)}
        current_idx = DEFAULT_CAM_INDEX
        current_label = ""
        last_seen_idx = state_holder.get("state", {}).get("camera_index", -1)
        last_seen_label = state_holder.get("state", {}).get("camera_label", "")
        cap = cv2.VideoCapture()
        if current_idx >= 0:
            cap = open_cam(current_idx, "")
            if cap.isOpened():
                logger.info("Opened camera index %s on startup (from INPUT_CAM_INDEX)", current_idx)
            else:
                logger.warning("Failed to open camera index %s on startup; waiting for selection", current_idx)
        else:
            logger.info(
                "No camera selected on startup; waiting for selection (initial state index=%s label='%s')",
                last_seen_idx,
                last_seen_label,
            )

        fail_count = 0
        ws_task = asyncio.create_task(ws_state_listener(state_holder))
        source_width = max(1, DEFAULT_WIDTH)
        source_height = max(1, DEFAULT_HEIGHT)
        output_width = source_width
        output_height = source_height
        if cap.isOpened():
            source_width, source_height = _capture_size(cap)
        cam: pyvirtualcam.Camera | None
        cam, output_width, output_height = _restart_virtual_camera(
            None,
            source_width,
            source_height,
            output_width,
            output_height,
        )
        try:
            while True:
                desired_idx = state_holder.get("state", {}).get("camera_index", -1)
                desired_label = state_holder.get("state", {}).get("camera_label", "")
                state_changed = desired_idx != last_seen_idx or desired_label != last_seen_label
                if state_changed:
                    last_seen_idx = desired_idx
                    last_seen_label = desired_label

                should_attempt = False
                now = time.monotonic()
                if desired_idx != -1:
                    if current_idx < 0:
                        if state_changed or (now - last_open_attempt) > 2.0:
                            should_attempt = True
                    elif desired_idx != current_idx and state_changed:
                        should_attempt = True

                if should_attempt:
                    last_open_attempt = now
                    logger.info("Switching camera to index %s label '%s'", desired_idx, desired_label)
                    cap.release()
                    new_cap = open_cam(desired_idx, desired_label)
                    if new_cap.isOpened():
                        cap = new_cap
                        current_idx = desired_idx
                        current_label = desired_label
                        new_w, new_h = _capture_size(cap)
                        if new_w != source_width or new_h != source_height:
                            source_width, source_height = new_w, new_h
                            cam, output_width, output_height = _restart_virtual_camera(
                                cam,
                                source_width,
                                source_height,
                                output_width,
                                output_height,
                            )
                    else:
                        logger.warning(
                            "Failed to open camera index %s label '%s'; keeping previous",
                            desired_idx,
                            desired_label,
                        )

                if not cap.isOpened():
                    blank = np.zeros((output_height, output_width, 3), dtype=np.uint8)
                    overlayed = draw_overlay(blank, state_holder.get("state") or {})
                    cam.send(overlayed)
                    cam.sleep_until_next_frame()
                    await asyncio.sleep(0.05)
                    continue

                ret, frame = cap.read()
                if not ret or frame is None:
                    logger.warning("Camera frame grab failed")
                    fail_count += 1
                    if fail_count > 30:
                        logger.warning("Reopening camera after repeated failures")
                        cap.release()
                        if current_idx >= 0:
                            cap = open_cam(current_idx, "")
                            if cap.isOpened():
                                new_w, new_h = _capture_size(cap)
                                if new_w != source_width or new_h != source_height:
                                    source_width, source_height = new_w, new_h
                                    cam, output_width, output_height = _restart_virtual_camera(
                                        cam,
                                        source_width,
                                        source_height,
                                        output_width,
                                        output_height,
                                    )
                        fail_count = 0
                        await asyncio.sleep(0.1)
                        continue
                    await asyncio.sleep(0.01)
                    continue
                fail_count = 0
                frame_w, frame_h = _capture_size(cap, frame)
                if frame_w != source_width or frame_h != source_height:
                    source_width, source_height = frame_w, frame_h
                    cam, output_width, output_height = _restart_virtual_camera(
                        cam,
                        source_width,
                        source_height,
                        output_width,
                        output_height,
                    )
                if frame_w != output_width or frame_h != output_height:
                    frame = cv2.resize(frame, (output_width, output_height), interpolation=cv2.INTER_AREA)
                overlayed = draw_overlay(frame, state_holder.get("state") or {})
                cam.send(overlayed)
                cam.sleep_until_next_frame()
                try:
                    state_task = asyncio.create_task(fetch_state(client))
                    await asyncio.wait_for(asyncio.shield(state_task), timeout=POLL_INTERVAL)
                    state_holder["state"] = state_task.result() or state_holder.get("state") or {}
                except asyncio.TimeoutError:
                    pass
        finally:
            try:
                ws_task.cancel()
            except Exception:
                pass
            try:
                cap.release()
            except Exception:
                pass
            if cam is not None:
                try:
                    cam.close()
                except Exception:
                    pass


if __name__ == "__main__":
    asyncio.run(main())

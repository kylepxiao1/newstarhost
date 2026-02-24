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
try:
    from overlay_renderer import draw_overlay as optimized_draw_overlay
except Exception:
    from scripts.overlay_renderer import draw_overlay as optimized_draw_overlay
from pyvirtualcam import PixelFormat

API_BASE = os.environ.get("API_BASE", "https://newstarhost.fly.dev") # http://127.0.0.1:8000
DEFAULT_CAM_INDEX = int(os.environ.get("INPUT_CAM_INDEX", -1))  # -1 => auto-pick first working camera
DEFAULT_WIDTH = int(os.environ.get("CAM_WIDTH", 1920))
DEFAULT_HEIGHT = int(os.environ.get("CAM_HEIGHT", 1080))
FPS = int(os.environ.get("CAM_FPS", 30))
POLL_INTERVAL = float(os.environ.get("STATE_POLL_SECS", 5.0))
LIKE_OVERLAY_POLL_INTERVAL = float(os.environ.get("LIKE_OVERLAY_POLL_SECS", "5.0"))
WS_PATH = os.environ.get("STATE_WS_PATH", "/ws/state")
CAM_OPEN_RETRIES = int(os.environ.get("CAM_OPEN_RETRIES", 4))
CAM_OPEN_DELAY = float(os.environ.get("CAM_OPEN_DELAY", 0.35))
# Disabled by default to keep camera switches responsive during live runs.
CAM_PROBE_MAX_RES = os.environ.get("CAM_PROBE_MAX_RES", "0").strip().lower() not in {"0", "false", "no", "off"}
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


async def fetch_likes_overlay(client: httpx.AsyncClient) -> Dict:
    try:
        resp = await client.get(f"{API_BASE}/camera/likes/summary", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        _verbose_log("Failed to fetch likes overlay summary: %s", exc)
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


def _transform_overlay_only(
    frame: np.ndarray,
    overlayed: np.ndarray,
    rotation_deg: int,
    flip_x: bool,
    flip_y: bool,
) -> np.ndarray:
    """Transform only overlay pixels around frame center, leaving base frame unchanged."""
    rotation_norm = int(rotation_deg) % 360
    if rotation_norm == 0 and not flip_x and not flip_y:
        return overlayed
    h, w = frame.shape[:2]

    diff = cv2.absdiff(overlayed, frame)
    diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(diff_gray, 2, 255, cv2.THRESH_BINARY)
    points = cv2.findNonZero(mask)
    if points is None:
        return overlayed

    x, y, bw, bh = cv2.boundingRect(points)
    x2 = x + max(0, bw - 1)
    y2 = y + max(0, bh - 1)
    corners = np.array(
        [
            [x, y, 1.0],
            [x2, y, 1.0],
            [x, y2, 1.0],
            [x2, y2, 1.0],
        ],
        dtype=np.float32,
    ).T

    cx = (w - 1) / 2.0
    cy = (h - 1) / 2.0
    rot_2x3 = cv2.getRotationMatrix2D((cx, cy), -float(rotation_norm), 1.0)
    mat = np.vstack([rot_2x3, [0.0, 0.0, 1.0]]).astype(np.float32)

    if flip_x:
        flipx = np.array(
            [
                [-1.0, 0.0, 2.0 * cx],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float32,
        )
        mat = flipx @ mat
    if flip_y:
        flipy = np.array(
            [
                [1.0, 0.0, 0.0],
                [0.0, -1.0, 2.0 * cy],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float32,
        )
        mat = flipy @ mat

    transformed_corners = mat @ corners
    min_x = float(np.min(transformed_corners[0]))
    max_x = float(np.max(transformed_corners[0]))
    min_y = float(np.min(transformed_corners[1]))
    max_y = float(np.max(transformed_corners[1]))

    # Keep transformed overlays near their original top-left anchor when possible,
    # then clamp into frame bounds.
    target_min_x = float(x)
    target_min_y = float(y)

    def _anchored_clamped_shift(
        src_min: float, src_max: float, target_min: float, max_bound: float
    ) -> float:
        desired = target_min - src_min
        low = -src_min
        high = max_bound - src_max
        if low > high:
            # Overlay span exceeds bounds; center best-effort.
            return ((max_bound - (src_max - src_min)) / 2.0) - src_min
        return min(high, max(low, desired))

    dx = _anchored_clamped_shift(min_x, max_x, target_min_x, float(max(1, w - 1)))
    dy = _anchored_clamped_shift(min_y, max_y, target_min_y, float(max(1, h - 1)))
    if dx != 0.0 or dy != 0.0:
        shift = np.array(
            [
                [1.0, 0.0, dx],
                [0.0, 1.0, dy],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float32,
        )
        mat = shift @ mat

    mat_2x3 = mat[:2, :]

    transformed_overlay = cv2.warpAffine(
        overlayed,
        mat_2x3,
        (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    transformed_mask = cv2.warpAffine(
        mask,
        mat_2x3,
        (w, h),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )

    out = frame.copy()
    idx = transformed_mask > 0
    out[idx] = transformed_overlay[idx]
    return out


def draw_overlay(frame: np.ndarray, state: Dict, likes_overlay: Dict | None = None) -> np.ndarray:
    """
    Render overlays with resolution-aware sizing so text stays crisp at any resolution.
    """
    overlay = frame.copy()
    wins = state.get("win_counts") or {}
    enabled = set((state.get("enabled_dancers") or []))
    dancers = state.get("dancers") or []
    display_dancers = dancers if not enabled else [d for d in dancers if (d.get("name") or "") in enabled]
    overlays = state.get("overlay_states") or {
        "CenterDottedLine": False,
        "BurstOverlay": True,
        "BattleScore": True,
        "TotalLikesOverlay": True,
    }
    overlay_layouts = state.get("overlay_layouts") or {}
    font = cv2.FONT_HERSHEY_SIMPLEX

    # Scale elements based on frame height (smaller text)
    base_h = max(1, frame.shape[0])
    scale = max(0.7, (base_h / 720.0) * 0.9)
    thick = max(1, int(scale * 2))
    line_step = max(20, int(base_h / 38))

    def outlined_text(img: np.ndarray, text: str, org: tuple[int, int], color: tuple[int, int, int] = (255, 255, 255)) -> None:
        cv2.putText(img, text, org, font, scale, (0, 0, 0), thick + 1, cv2.LINE_AA)
        cv2.putText(img, text, org, font, scale, color, thick, cv2.LINE_AA)

    def _overlay_layout(name: str) -> tuple[int, int, float]:
        raw = overlay_layouts.get(name) if isinstance(overlay_layouts, dict) else None
        if not isinstance(raw, dict):
            return 0, 0, 1.0
        try:
            x_val = int(round(float(raw.get("x_offset", 0))))
        except Exception:
            x_val = 0
        try:
            y_val = int(round(float(raw.get("y_offset", 0))))
        except Exception:
            y_val = 0
        try:
            s_val = float(raw.get("scale", 1))
        except Exception:
            s_val = 1.0
        if s_val <= 0:
            s_val = 1.0
        s_val = max(0.1, min(8.0, s_val))
        return x_val, y_val, s_val

    if overlays.get("CenterDottedLine", False):
        x_off, y_off, s_off = _overlay_layout("CenterDottedLine")
        center_x = max(0, min(frame.shape[1] - 1, (frame.shape[1] // 2) + x_off))
        dash = max(10, int(line_step * 0.9 * s_off))   # longer dashes
        gap = max(8, int(line_step * 0.7 * s_off))   # larger gaps
        white_thick = max(2, int(scale * 3.2 * s_off))  # thicker
        black_thick = max(1, int(scale * 1.8 * s_off))
        cycle = max(1, dash + gap)
        y = y_off % cycle
        while y < frame.shape[0]:
            y2 = min(y + dash, frame.shape[0])
            cv2.line(overlay, (center_x, y), (center_x, y2), (255, 255, 255), white_thick)
            cv2.line(overlay, (center_x, y), (center_x, y2), (0, 0, 0), black_thick)
            y += dash + gap

    if overlays.get("BurstOverlay", True):
        x_off, y_off, s_off = _overlay_layout("BurstOverlay")
        mask = np.zeros_like(frame)
        rad = max(6, int(min(frame.shape[0], frame.shape[1]) * 0.18 * s_off))
        c1 = (
            max(0, min(frame.shape[1] - 1, int(frame.shape[1] * 0.25) + x_off)),
            max(0, min(frame.shape[0] - 1, int(frame.shape[0] * 0.25) + y_off)),
        )
        c2 = (
            max(0, min(frame.shape[1] - 1, int(frame.shape[1] * 0.75) + x_off)),
            max(0, min(frame.shape[0] - 1, int(frame.shape[0] * 0.75) + y_off)),
        )
        cv2.circle(mask, c1, rad, (0, 128, 255), -1)
        cv2.circle(mask, c2, rad, (255, 64, 128), -1)
        overlay = cv2.addWeighted(overlay, 0.9, mask, 0.1, 0)

    if overlays.get("BattleScore", True):
        x_off, y_off, s_off = _overlay_layout("BattleScore")
        bs_scale = max(0.25, scale * s_off)
        bs_thick = max(1, int(thick * s_off))
        y = max(16, int(50 * scale) + y_off)
        x = max(8, 40 + x_off)
        step = max(14, int(36 * scale * s_off))
        for dancer in display_dancers:
            name = dancer.get("name") or "Waiting"
            text = f"{name}: {wins.get(name, 0)} wins"
            cv2.putText(overlay, text, (x, y), font, bs_scale, (0, 0, 0), bs_thick + 1, cv2.LINE_AA)
            cv2.putText(overlay, text, (x, y), font, bs_scale, (255, 255, 255), bs_thick, cv2.LINE_AA)
            y += step

    if overlays.get("TotalLikesOverlay", True):
        x_off, y_off, s_off = _overlay_layout("TotalLikesOverlay")
        likes = likes_overlay if isinstance(likes_overlay, dict) else {}
        total_likes_raw = likes.get("total_likes", 0)
        try:
            total_likes = max(0, int(float(str(total_likes_raw).strip() or "0")))
        except Exception:
            total_likes = 0
        goal_text = str(likes.get("like_goal") or "").strip()
        goal_value_raw = likes.get("goal_value", None)
        try:
            goal_value = int(float(str(goal_value_raw).strip())) if goal_value_raw is not None else 0
        except Exception:
            goal_value = 0
        if goal_value <= 0 and goal_text:
            digits = "".join(ch for ch in goal_text if ch.isdigit())
            try:
                goal_value = int(digits) if digits else 0
            except Exception:
                goal_value = 0
        goal_value = max(0, goal_value)
        progress_raw = likes.get("progress")
        if goal_value > 0:
            try:
                progress = float(progress_raw) if progress_raw is not None else (float(total_likes) / float(goal_value))
            except Exception:
                progress = float(total_likes) / float(goal_value)
            progress = max(0.0, min(1.0, progress))
        else:
            progress = 0.0
        prize_text = str(likes.get("prize") or "").strip()
        goal_display = f"{goal_value:,}" if goal_value > 0 else (goal_text or "0")
        likes_line = f"{total_likes:,} / {goal_display} Likes"
        if prize_text:
            likes_line = f"{prize_text} - {likes_line}"

        max_text_width = max(120, int(frame.shape[1] - 40))
        text_scale = max(0.3, scale * s_off)
        text_thick = max(1, int(thick * s_off))
        (text_w, text_h), _ = cv2.getTextSize(likes_line, font, text_scale, text_thick)
        if text_w > max_text_width:
            shrink_ratio = max_text_width / float(max(1, text_w))
            text_scale = max(0.25, text_scale * shrink_ratio)
            (text_w, text_h), _ = cv2.getTextSize(likes_line, font, text_scale, text_thick)

        text_x = int((frame.shape[1] - text_w) / 2) + x_off
        text_x = max(8, min(frame.shape[1] - text_w - 8, text_x))
        text_y = max(text_h + 18, int(54 * scale) + y_off)
        text_y = min(frame.shape[0] - 8, text_y)
        # TikTok-style peach neon headline treatment.
        cv2.putText(overlay, likes_line, (text_x, text_y), font, text_scale, (0, 0, 0), text_thick + 4, cv2.LINE_AA)
        cv2.putText(overlay, likes_line, (text_x, text_y), font, text_scale, (150, 196, 255), text_thick + 2, cv2.LINE_AA)
        cv2.putText(overlay, likes_line, (text_x, text_y), font, text_scale, (255, 255, 255), text_thick, cv2.LINE_AA)

        if goal_value > 0:
            def _draw_pill(img: np.ndarray, x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int], thickness: int = -1) -> None:
                if x2 <= x1 or y2 <= y1:
                    return
                radius = max(1, int((y2 - y1) / 2))
                if (x2 - x1) <= radius * 2:
                    cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness, lineType=cv2.LINE_AA)
                    return
                cv2.rectangle(img, (x1 + radius, y1), (x2 - radius, y2), color, thickness, lineType=cv2.LINE_AA)
                cv2.circle(img, (x1 + radius, y1 + radius), radius, color, thickness, lineType=cv2.LINE_AA)
                cv2.circle(img, (x2 - radius, y1 + radius), radius, color, thickness, lineType=cv2.LINE_AA)

            bar_h = max(14, int(20 * scale * s_off))
            bar_y1 = text_y + max(8, int(12 * scale * s_off))
            bar_y2 = bar_y1 + bar_h
            bar_w = min(max(180, int((text_w + int(42 * text_scale)) * max(0.75, s_off))), max(180, int(frame.shape[1] * 0.72)))
            bar_x1 = int((frame.shape[1] - bar_w) / 2) + x_off
            bar_x1 = max(8, min(frame.shape[1] - bar_w - 8, bar_x1))
            bar_x2 = min(frame.shape[1] - 8, bar_x1 + bar_w)

            _draw_pill(overlay, bar_x1, bar_y1, bar_x2, bar_y2, (30, 41, 59), -1)
            fill_w = int((bar_x2 - bar_x1) * progress)
            if fill_w > 0:
                fill_x2 = min(bar_x2, bar_x1 + fill_w)
                _draw_pill(overlay, bar_x1, bar_y1, fill_x2, bar_y2, (132, 185, 255), -1)
                # Top highlight for a glossy progress look.
                highlight_y = bar_y1 + max(1, int(bar_h * 0.3))
                cv2.line(
                    overlay,
                    (bar_x1 + 2, highlight_y),
                    (max(bar_x1 + 2, fill_x2 - 2), highlight_y),
                    (255, 238, 248),
                    max(1, text_thick - 1),
                    cv2.LINE_AA,
                )
            _draw_pill(overlay, bar_x1, bar_y1, bar_x2, bar_y2, (128, 222, 255), max(1, text_thick - 1))

            pct_text = f"{int(round(progress * 100.0))}%"
            pct_scale = max(0.32, text_scale * 0.72)
            (pct_w, pct_h), _ = cv2.getTextSize(pct_text, font, pct_scale, max(1, text_thick - 1))
            pct_x = int((bar_x1 + bar_x2 - pct_w) / 2)
            pct_y = bar_y1 + int((bar_h + pct_h) / 2) - 2
            cv2.putText(overlay, pct_text, (pct_x, pct_y), font, pct_scale, (0, 0, 0), max(1, text_thick), cv2.LINE_AA)
            cv2.putText(overlay, pct_text, (pct_x, pct_y), font, pct_scale, (255, 255, 255), max(1, text_thick - 1), cv2.LINE_AA)
    raw_rot = state.get("overlay_rotation_deg", 0)
    try:
        rot = int(raw_rot)
    except Exception:
        rot = 0
    rot = rot % 360
    if rot not in (0, 90, 180, 270):
        rot = int(round(rot / 90.0) * 90) % 360
    flip_x = bool(state.get("overlay_flip_x", False))
    flip_y = bool(state.get("overlay_flip_y", False))
    if rot or flip_x or flip_y:
        return _transform_overlay_only(frame, overlay, rot, flip_x, flip_y)
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
                    best = _probe_best_resolution(cap, label, (actual_w, actual_h)) if CAM_PROBE_MAX_RES else None
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
        likes_overlay_holder: Dict = {"summary": await fetch_likes_overlay(client)}
        next_like_overlay_poll = 0.0
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

                if LIKE_OVERLAY_POLL_INTERVAL >= 0:
                    now_poll = time.monotonic()
                    if now_poll >= next_like_overlay_poll:
                        likes_overlay_holder["summary"] = await fetch_likes_overlay(client)
                        next_like_overlay_poll = now_poll + max(0.25, LIKE_OVERLAY_POLL_INTERVAL)

                if not cap.isOpened():
                    blank = np.zeros((output_height, output_width, 3), dtype=np.uint8)
                    overlayed = optimized_draw_overlay(
                        blank,
                        state_holder.get("state") or {},
                        likes_overlay_holder.get("summary") or {},
                    )
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
                overlayed = optimized_draw_overlay(
                    frame,
                    state_holder.get("state") or {},
                    likes_overlay_holder.get("summary") or {},
                )
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

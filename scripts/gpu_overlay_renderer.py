import logging
from typing import Dict

import cv2
import numpy as np

try:
    import glfw
except Exception:
    glfw = None

try:
    import skia
except Exception:
    skia = None


DEFAULT_OVERLAYS = {
    "CenterDottedLine": False,
    "BurstOverlay": True,
    "BattleScore": True,
    "TotalLikesOverlay": True,
}


def _normalize_overlay_transform(state: Dict) -> tuple[int, bool, bool]:
    raw_rot = state.get("overlay_rotation_deg", 0)
    try:
        rot = int(raw_rot)
    except Exception:
        rot = 0
    rot = rot % 360
    if rot not in (0, 90, 180, 270):
        rot = int(round(rot / 90.0) * 90) % 360
    return rot, bool(state.get("overlay_flip_x", False)), bool(state.get("overlay_flip_y", False))


def _overlay_layout(layouts: Dict, name: str) -> tuple[int, int, float]:
    raw = layouts.get(name) if isinstance(layouts, dict) else None
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


def _normalize_likes_payload(likes_overlay: Dict | None) -> Dict:
    likes = likes_overlay if isinstance(likes_overlay, dict) else {}
    total_likes_raw = likes.get("total_likes", 0)
    try:
        total_likes = max(0, int(float(str(total_likes_raw).strip() or "0")))
    except Exception:
        total_likes = 0

    goal_text = str(likes.get("like_goal") or "").strip()
    goal_value_raw = likes.get("goal_value")
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

    return {
        "total_likes": total_likes,
        "goal_value": goal_value,
        "progress": progress,
        "likes_line": likes_line,
    }


class SkiaGpuOverlayRenderer:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("virtual-cam")
        self._window = None
        self._gr_context = None
        self._surface = None
        self._surface_w = 0
        self._surface_h = 0
        self._font_cache: dict[int, object] = {}
        self._transform_warned = False
        self._fail_count = 0
        self._available = bool(glfw is not None and skia is not None)
        self._reason = ""
        if not self._available:
            self._reason = "missing optional dependencies (skia-python + glfw)"

    def is_available(self) -> bool:
        return self._available

    def unavailable_reason(self) -> str:
        return self._reason or "unknown"

    def pipeline_mode(self) -> str:
        if self._available:
            return "GPU-native Skia/OpenGL (text + primitives on GPU; frame readback for virtual cam)"
        return f"CPU fallback (Skia unavailable: {self.unavailable_reason()})"

    def _color(self, bgr: tuple[int, int, int], alpha: int = 255):
        b, g, r = int(bgr[0]), int(bgr[1]), int(bgr[2])
        return skia.ColorSetARGB(int(alpha), r, g, b)

    def _font(self, size_px: float):
        key = max(8, int(round(size_px)))
        cached = self._font_cache.get(key)
        if cached is not None:
            return cached
        font = skia.Font(None, float(key))
        self._font_cache[key] = font
        return font

    def _paint_fill(self, bgr: tuple[int, int, int], alpha: int = 255):
        return skia.Paint(
            Color=self._color(bgr, alpha),
            AntiAlias=True,
            Style=skia.Paint.kFill_Style,
        )

    def _paint_stroke(self, bgr: tuple[int, int, int], width: float, alpha: int = 255):
        return skia.Paint(
            Color=self._color(bgr, alpha),
            AntiAlias=True,
            Style=skia.Paint.kStroke_Style,
            StrokeWidth=float(max(1.0, width)),
        )

    def _ensure_context(self) -> None:
        if not self._available:
            raise RuntimeError(self.unavailable_reason())
        if self._window is not None and self._gr_context is not None:
            glfw.make_context_current(self._window)
            return
        if not glfw.init():
            raise RuntimeError("glfw.init failed")
        glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
        glfw.window_hint(glfw.DOUBLEBUFFER, glfw.FALSE)
        window = glfw.create_window(1, 1, "overlay-gpu", None, None)
        if window is None:
            raise RuntimeError("glfw.create_window failed")
        glfw.make_context_current(window)
        glfw.swap_interval(0)
        gr_context = skia.GrDirectContext.MakeGL()
        if gr_context is None:
            raise RuntimeError("skia GrDirectContext.MakeGL failed")
        self._window = window
        self._gr_context = gr_context

    def _ensure_surface(self, width: int, height: int) -> None:
        if self._surface is not None and self._surface_w == width and self._surface_h == height:
            return
        info = skia.ImageInfo.Make(
            int(width),
            int(height),
            skia.ColorType.kRGBA_8888_ColorType,
            skia.AlphaType.kPremul_AlphaType,
        )
        surface = skia.Surface.MakeRenderTarget(self._gr_context, skia.Budgeted.kNo, info)
        if surface is None:
            raise RuntimeError("skia Surface.MakeRenderTarget failed")
        self._surface = surface
        self._surface_w = int(width)
        self._surface_h = int(height)

    def _image_from_rgba(self, rgba: np.ndarray):
        try:
            return skia.Image.fromarray(rgba, colorType=skia.ColorType.kRGBA_8888_ColorType)
        except Exception:
            try:
                info = skia.ImageInfo.Make(
                    int(rgba.shape[1]),
                    int(rgba.shape[0]),
                    skia.ColorType.kRGBA_8888_ColorType,
                    skia.AlphaType.kPremul_AlphaType,
                )
                data = skia.Data.MakeWithoutCopy(rgba)
                return skia.Image.MakeRasterData(info, data, int(rgba.strides[0]))
            except Exception:
                return None

    def _draw_outlined_text(
        self,
        canvas,
        text: str,
        x: float,
        y: float,
        font_size: float,
        text_color: tuple[int, int, int],
        outline_color: tuple[int, int, int] = (0, 0, 0),
        outline_px: int = 2,
    ) -> None:
        font = self._font(font_size)
        outline_paint = self._paint_fill(outline_color, 255)
        text_paint = self._paint_fill(text_color, 255)
        off = max(1, int(outline_px))
        for ox, oy in ((-off, 0), (off, 0), (0, -off), (0, off)):
            canvas.drawString(text, float(x + ox), float(y + oy), font, outline_paint)
        canvas.drawString(text, float(x), float(y), font, text_paint)

    def _draw_pill(self, canvas, x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int], stroke: int = -1) -> None:
        if x2 <= x1 or y2 <= y1:
            return
        radius = max(1.0, float((y2 - y1) * 0.5))
        rect = skia.Rect.MakeLTRB(float(x1), float(y1), float(x2), float(y2))
        rr = skia.RRect.MakeRectXY(rect, radius, radius)
        if stroke < 0:
            paint = self._paint_fill(color, 255)
        else:
            paint = self._paint_stroke(color, float(max(1, stroke)), 255)
        canvas.drawRRect(rr, paint)

    def render(self, frame: np.ndarray, state: Dict, likes_overlay: Dict | None = None) -> np.ndarray | None:
        if not self._available:
            return None
        rot, flip_x, flip_y = _normalize_overlay_transform(state)
        if rot or flip_x or flip_y:
            if not self._transform_warned:
                self._transform_warned = True
                self._logger.info("Skia GPU renderer fallback: rotation/flip active, using CPU overlay renderer")
            return None

        try:
            h, w = frame.shape[:2]
            if h <= 0 or w <= 0:
                return frame
            self._ensure_context()
            self._ensure_surface(w, h)
            canvas = self._surface.getCanvas()

            rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
            base_image = self._image_from_rgba(rgba)
            if base_image is None:
                return None
            canvas.drawImage(base_image, 0, 0)

            overlays = state.get("overlay_states") or DEFAULT_OVERLAYS
            layouts = state.get("overlay_layouts") or {}
            wins = state.get("win_counts") or {}
            dancers = state.get("dancers") or []
            enabled = set(state.get("enabled_dancers") or [])
            display_dancers = dancers if not enabled else [d for d in dancers if (d.get("name") or "") in enabled]

            base_h = max(1, h)
            scale = max(0.7, (base_h / 720.0) * 0.9)
            thick = max(1, int(scale * 2))
            line_step = max(20, int(base_h / 38))

            if overlays.get("CenterDottedLine", False):
                x_off, y_off, s_off = _overlay_layout(layouts, "CenterDottedLine")
                center_x = max(0, min(w - 1, (w // 2) + x_off))
                dash = max(10, int(line_step * 0.9 * s_off))
                gap = max(8, int(line_step * 0.7 * s_off))
                white_thick = max(2, int(scale * 3.2 * s_off))
                black_thick = max(1, int(scale * 1.8 * s_off))
                cycle = max(1, dash + gap)
                y = y_off % cycle
                white = self._paint_stroke((255, 255, 255), float(white_thick), 255)
                black = self._paint_stroke((0, 0, 0), float(black_thick), 255)
                while y < h:
                    y2 = min(h - 1, y + dash)
                    canvas.drawLine(float(center_x), float(y), float(center_x), float(y2), white)
                    canvas.drawLine(float(center_x), float(y), float(center_x), float(y2), black)
                    y += cycle

            if overlays.get("BurstOverlay", True):
                x_off, y_off, s_off = _overlay_layout(layouts, "BurstOverlay")
                rad = max(6, int(min(h, w) * 0.18 * s_off))
                c1 = (max(0, min(w - 1, int(w * 0.25) + x_off)), max(0, min(h - 1, int(h * 0.25) + y_off)))
                c2 = (max(0, min(w - 1, int(w * 0.75) + x_off)), max(0, min(h - 1, int(h * 0.75) + y_off)))
                canvas.drawCircle(float(c1[0]), float(c1[1]), float(rad), self._paint_fill((0, 128, 255), 26))
                canvas.drawCircle(float(c2[0]), float(c2[1]), float(rad), self._paint_fill((255, 64, 128), 26))

            if overlays.get("BattleScore", True):
                x_off, y_off, s_off = _overlay_layout(layouts, "BattleScore")
                font_size = max(11.0, float(24.0 * scale * s_off))
                y = max(16, int(50 * scale) + y_off)
                x = max(8, 40 + x_off)
                step = max(14, int(36 * scale * s_off))
                for dancer in display_dancers:
                    name = str(dancer.get("name") or "Waiting")
                    try:
                        win_count = int(wins.get(name, 0))
                    except Exception:
                        win_count = 0
                    text = f"{name}: {win_count} wins"
                    self._draw_outlined_text(
                        canvas,
                        text,
                        float(x),
                        float(y),
                        font_size=font_size,
                        text_color=(255, 255, 255),
                        outline_px=max(1, thick // 2 + 1),
                    )
                    y += step

            if overlays.get("TotalLikesOverlay", True):
                x_off, y_off, s_off = _overlay_layout(layouts, "TotalLikesOverlay")
                likes_norm = _normalize_likes_payload(likes_overlay)
                likes_line = str(likes_norm.get("likes_line") or "").strip()
                goal_value = int(likes_norm.get("goal_value", 0) or 0)
                progress = float(likes_norm.get("progress", 0.0) or 0.0)

                if likes_line:
                    text_scale = max(0.3, scale * s_off)
                    font_size = max(11.0, 24.0 * text_scale)
                    text_thick = max(1, int(thick * s_off))
                    text_w = int(max(1, len(likes_line)) * font_size * 0.56)
                    text_h = int(font_size)
                    text_x = int((w - text_w) * 0.5) + x_off
                    text_x = max(8, min(w - text_w - 8, text_x))
                    text_y = max(text_h + 18, int(54 * scale) + y_off)
                    text_y = min(h - 8, text_y)
                    self._draw_outlined_text(
                        canvas,
                        likes_line,
                        float(text_x),
                        float(text_y),
                        font_size=font_size,
                        text_color=(255, 255, 255),
                        outline_px=max(1, text_thick + 1),
                    )

                    if goal_value > 0:
                        bar_h = max(14, int(20 * scale * s_off))
                        bar_y1 = text_y + max(8, int(12 * scale * s_off))
                        bar_y2 = bar_y1 + bar_h
                        bar_w = min(
                            max(180, int((text_w + int(42 * text_scale)) * max(0.75, s_off))),
                            max(180, int(w * 0.72)),
                        )
                        bar_x1 = int((w - bar_w) * 0.5) + x_off
                        bar_x1 = max(8, min(w - bar_w - 8, bar_x1))
                        bar_x2 = min(w - 8, bar_x1 + bar_w)

                        self._draw_pill(canvas, bar_x1, bar_y1, bar_x2, bar_y2, (30, 41, 59), -1)
                        fill_w = int((bar_x2 - bar_x1) * progress)
                        if fill_w > 0:
                            fill_x2 = min(bar_x2, bar_x1 + fill_w)
                            self._draw_pill(canvas, bar_x1, bar_y1, fill_x2, bar_y2, (132, 185, 255), -1)
                        self._draw_pill(canvas, bar_x1, bar_y1, bar_x2, bar_y2, (128, 222, 255), max(1, text_thick - 1))

                        pct_text = f"{int(round(progress * 100.0))}%"
                        pct_font = max(10.0, font_size * 0.72)
                        pct_w = int(max(1, len(pct_text)) * pct_font * 0.56)
                        pct_x = int((bar_x1 + bar_x2 - pct_w) * 0.5)
                        pct_y = bar_y1 + int((bar_h + pct_font) * 0.5) - 2
                        self._draw_outlined_text(
                            canvas,
                            pct_text,
                            float(pct_x),
                            float(pct_y),
                            font_size=pct_font,
                            text_color=(255, 255, 255),
                            outline_px=max(1, text_thick),
                        )

            if self._gr_context is not None:
                self._gr_context.flushAndSubmit()
            image = self._surface.makeImageSnapshot()
            out_rgba = image.toarray(colorType=skia.ColorType.kRGBA_8888_ColorType)
            if out_rgba is None:
                return None
            out_rgba = np.asarray(out_rgba, dtype=np.uint8)
            if out_rgba.ndim != 3 or out_rgba.shape[2] != 4:
                return None
            return cv2.cvtColor(out_rgba, cv2.COLOR_RGBA2BGR)
        except Exception as exc:
            self._fail_count += 1
            if self._fail_count >= 3:
                self._available = False
                self._reason = f"renderer errors: {exc}"
            self._logger.warning("Skia GPU overlay render failed (%s); using CPU fallback", exc)
            return None

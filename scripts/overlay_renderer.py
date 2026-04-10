from __future__ import annotations

import os
import cv2
import numpy as np
from typing import Dict


DEFAULT_OVERLAYS = {
    "CenterDottedLine": False,
    "BurstOverlay": True,
    "BattleScore": True,
    "TotalLikesOverlay": True,
}


def _parse_gpu_mode(raw: str) -> str:
    token = (raw or "").strip().lower()
    if token in {"1", "true", "yes", "on", "enable", "enabled"}:
        return "on"
    if token in {"0", "false", "no", "off", "disable", "disabled"}:
        return "off"
    if token in {"", "auto", "default"}:
        return "auto"
    return "auto"


OVERLAY_GPU_MODE_RAW = os.environ.get("OVERLAY_GPU_MODE", "auto").strip()
OVERLAY_GPU_MODE = _parse_gpu_mode(OVERLAY_GPU_MODE_RAW)
OVERLAY_GPU_MIN_PIXELS = max(0, int(os.environ.get("OVERLAY_GPU_MIN_PIXELS", "180000")))


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


def _overlay_transform_matrix(frame_w: int, frame_h: int, rot: int, flip_x: bool, flip_y: bool) -> np.ndarray:
    cx = (frame_w - 1) / 2.0
    cy = (frame_h - 1) / 2.0
    rot_2x3 = cv2.getRotationMatrix2D((cx, cy), -float(rot), 1.0)
    mat = np.vstack([rot_2x3, [0.0, 0.0, 1.0]]).astype(np.float32)
    if flip_x:
        mat = (
            np.array(
                [
                    [-1.0, 0.0, 2.0 * cx],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0],
                ],
                dtype=np.float32,
            )
            @ mat
        )
    if flip_y:
        mat = (
            np.array(
                [
                    [1.0, 0.0, 0.0],
                    [0.0, -1.0, 2.0 * cy],
                    [0.0, 0.0, 1.0],
                ],
                dtype=np.float32,
            )
            @ mat
        )
    return mat


def _anchored_clamped_shift(src_min: float, src_max: float, target_min: float, max_bound: float) -> float:
    desired = target_min - src_min
    low = -src_min
    high = max_bound - src_max
    if low > high:
        return ((max_bound - (src_max - src_min)) / 2.0) - src_min
    return min(high, max(low, desired))


def _transform_bbox(base_mat: np.ndarray, bbox: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = bbox
    corners = np.array(
        [
            [x1, y1, 1.0],
            [x2, y1, 1.0],
            [x1, y2, 1.0],
            [x2, y2, 1.0],
        ],
        dtype=np.float32,
    ).T
    transformed = base_mat @ corners
    min_x = float(np.min(transformed[0]))
    max_x = float(np.max(transformed[0]))
    min_y = float(np.min(transformed[1]))
    max_y = float(np.max(transformed[1]))
    return min_x, min_y, max_x, max_y


def _union_bbox(boxes: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int] | None:
    if not boxes:
        return None
    min_x = min(b[0] for b in boxes)
    min_y = min(b[1] for b in boxes)
    max_x = max(b[2] for b in boxes)
    max_y = max(b[3] for b in boxes)
    return min_x, min_y, max_x, max_y


def _resolve_group_anchor_shift(
    base_mat: np.ndarray,
    source_boxes: list[tuple[int, int, int, int]],
    frame_w: int,
    frame_h: int,
) -> tuple[int, int]:
    src_union = _union_bbox(source_boxes)
    if src_union is None:
        return 0, 0
    src_min_x, src_min_y, _, _ = src_union

    transformed_boxes = [_transform_bbox(base_mat, box) for box in source_boxes]
    if not transformed_boxes:
        return 0, 0
    t_min_x = min(box[0] for box in transformed_boxes)
    t_min_y = min(box[1] for box in transformed_boxes)
    t_max_x = max(box[2] for box in transformed_boxes)
    t_max_y = max(box[3] for box in transformed_boxes)

    dx = _anchored_clamped_shift(t_min_x, t_max_x, float(src_min_x), float(max(0, frame_w - 1)))
    dy = _anchored_clamped_shift(t_min_y, t_max_y, float(src_min_y), float(max(0, frame_h - 1)))

    # Keep integer rasterization from nudging the final union out-of-bounds.
    shift_x = int(round(dx))
    shift_y = int(round(dy))

    min_x_i = int(np.floor(t_min_x + shift_x))
    max_x_i = int(np.ceil(t_max_x + shift_x))
    if min_x_i < 0:
        shift_x += -min_x_i
    if max_x_i > (frame_w - 1):
        shift_x -= max_x_i - (frame_w - 1)

    min_y_i = int(np.floor(t_min_y + shift_y))
    max_y_i = int(np.ceil(t_max_y + shift_y))
    if min_y_i < 0:
        shift_y += -min_y_i
    if max_y_i > (frame_h - 1):
        shift_y -= max_y_i - (frame_h - 1)

    return shift_x, shift_y


def _apply_orientation(image: np.ndarray, rot: int, flip_x: bool, flip_y: bool) -> np.ndarray:
    out = image
    if rot == 90:
        out = cv2.rotate(out, cv2.ROTATE_90_CLOCKWISE)
    elif rot == 180:
        out = cv2.rotate(out, cv2.ROTATE_180)
    elif rot == 270:
        out = cv2.rotate(out, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if flip_x:
        out = cv2.flip(out, 1)
    if flip_y:
        out = cv2.flip(out, 0)
    return out


def _blit_opaque(
    dst: np.ndarray,
    dst_mask: np.ndarray | None,
    sprite: np.ndarray,
    sprite_mask: np.ndarray,
    dst_x: int,
    dst_y: int,
) -> bool:
    dh, dw = dst.shape[:2]
    sh, sw = sprite.shape[:2]
    x1 = max(0, dst_x)
    y1 = max(0, dst_y)
    x2 = min(dw, dst_x + sw)
    y2 = min(dh, dst_y + sh)
    if x1 >= x2 or y1 >= y2:
        return False

    sx1 = x1 - dst_x
    sy1 = y1 - dst_y
    sx2 = sx1 + (x2 - x1)
    sy2 = sy1 + (y2 - y1)

    sprite_roi = sprite[sy1:sy2, sx1:sx2]
    mask_roi = sprite_mask[sy1:sy2, sx1:sx2]
    if cv2.countNonZero(mask_roi) == 0:
        return False

    dst_roi = dst[y1:y2, x1:x2]
    cv2.copyTo(sprite_roi, mask_roi, dst_roi)
    if dst_mask is not None:
        mask_dst_roi = dst_mask[y1:y2, x1:x2]
        cv2.bitwise_or(mask_dst_roi, mask_roi, dst=mask_dst_roi)
    return True


def _blend_masked(dst: np.ndarray, sprite: np.ndarray, sprite_mask: np.ndarray, dst_x: int, dst_y: int, alpha: float) -> None:
    if alpha <= 0.0:
        return
    dh, dw = dst.shape[:2]
    sh, sw = sprite.shape[:2]
    x1 = max(0, dst_x)
    y1 = max(0, dst_y)
    x2 = min(dw, dst_x + sw)
    y2 = min(dh, dst_y + sh)
    if x1 >= x2 or y1 >= y2:
        return

    sx1 = x1 - dst_x
    sy1 = y1 - dst_y
    sx2 = sx1 + (x2 - x1)
    sy2 = sy1 + (y2 - y1)

    sprite_roi = sprite[sy1:sy2, sx1:sx2]
    mask_roi = sprite_mask[sy1:sy2, sx1:sx2]
    if cv2.countNonZero(mask_roi) == 0:
        return

    dst_roi = dst[y1:y2, x1:x2]
    blended = cv2.addWeighted(dst_roi, 1.0 - alpha, sprite_roi, alpha, 0.0)
    cv2.copyTo(blended, mask_roi, dst_roi)


def _crop_to_mask(layer: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int, int]] | None:
    points = cv2.findNonZero(mask)
    if points is None:
        return None
    x, y, bw, bh = cv2.boundingRect(points)
    if bw <= 0 or bh <= 0:
        return None
    x2 = x + bw - 1
    y2 = y + bh - 1
    return layer[y : y + bh, x : x + bw].copy(), mask[y : y + bh, x : x + bw].copy(), (x, y, x2, y2)


def _ops_union_bounds(ops: list[tuple[np.ndarray, np.ndarray, int, int, float]]) -> tuple[int, int, int, int] | None:
    if not ops:
        return None
    min_x = min(op[2] for op in ops)
    min_y = min(op[3] for op in ops)
    max_x = max(op[2] + op[0].shape[1] - 1 for op in ops)
    max_y = max(op[3] + op[0].shape[0] - 1 for op in ops)
    return min_x, min_y, max_x, max_y


def _clamp_shift_to_bounds(shift: int, src_min: int, src_max: int, max_bound: int) -> int:
    low = -src_min
    high = max_bound - src_max
    if low > high:
        return int(round((low + high) / 2.0))
    return int(min(high, max(low, shift)))


def _clamp_op_origin(x: int, y: int, op_w: int, op_h: int, frame_w: int, frame_h: int) -> tuple[int, int]:
    cx = int(round(x))
    cy = int(round(y))
    if frame_w > 0:
        if op_w <= frame_w:
            cx = max(0, min(frame_w - op_w, cx))
        else:
            # If an overlay is wider than frame, allow panning across full range.
            cx = max(frame_w - op_w, min(0, cx))
    if frame_h > 0:
        if op_h <= frame_h:
            cy = max(0, min(frame_h - op_h, cy))
        else:
            # If an overlay is taller than frame, allow panning across full range.
            cy = max(frame_h - op_h, min(0, cy))
    return cx, cy


def _opencl_runtime_state() -> tuple[bool, bool]:
    available = False
    enabled = False
    try:
        if hasattr(cv2, "ocl"):
            if hasattr(cv2.ocl, "haveOpenCL"):
                available = bool(cv2.ocl.haveOpenCL())
            if hasattr(cv2.ocl, "useOpenCL"):
                enabled = bool(cv2.ocl.useOpenCL())
    except Exception:
        available = False
        enabled = False
    return available, enabled


class OverlayRenderer:
    def __init__(self) -> None:
        self._gpu_mode = OVERLAY_GPU_MODE
        self._gpu_mode_raw = OVERLAY_GPU_MODE_RAW or "auto"
        self._static_key: tuple | None = None
        self._dynamic_key: tuple | None = None
        self._static_source_boxes: list[tuple[int, int, int, int]] = []
        self._dynamic_source_boxes: list[tuple[int, int, int, int]] = []
        self._center_ops: list[tuple[np.ndarray, np.ndarray, int, int, float]] = []
        self._burst_ops: list[tuple[np.ndarray, np.ndarray, int, int, float]] = []
        self._dynamic_ops: list[tuple[np.ndarray, np.ndarray, int, int, float]] = []
        self._static_opaque_layer_cpu: np.ndarray | None = None
        self._static_opaque_mask_cpu: np.ndarray | None = None
        self._static_opaque_layer_u: cv2.UMat | None = None
        self._static_opaque_mask_u: cv2.UMat | None = None
        self._static_opaque_x = 0
        self._static_opaque_y = 0
        self._static_blend_layer_cpu: np.ndarray | None = None
        self._static_blend_mask_cpu: np.ndarray | None = None
        self._static_blend_layer_u: cv2.UMat | None = None
        self._static_blend_mask_u: cv2.UMat | None = None
        self._static_blend_x = 0
        self._static_blend_y = 0
        self._static_blend_alpha = 0.0
        self._dynamic_layer_cpu: np.ndarray | None = None
        self._dynamic_mask_cpu: np.ndarray | None = None
        self._dynamic_layer_u: cv2.UMat | None = None
        self._dynamic_mask_u: cv2.UMat | None = None
        self._dynamic_x = 0
        self._dynamic_y = 0
        self._font = cv2.FONT_HERSHEY_SIMPLEX

    def _invalidate_static_gpu_cache(self) -> None:
        self._static_opaque_layer_cpu = None
        self._static_opaque_mask_cpu = None
        self._static_opaque_layer_u = None
        self._static_opaque_mask_u = None
        self._static_opaque_x = 0
        self._static_opaque_y = 0
        self._static_blend_layer_cpu = None
        self._static_blend_mask_cpu = None
        self._static_blend_layer_u = None
        self._static_blend_mask_u = None
        self._static_blend_x = 0
        self._static_blend_y = 0
        self._static_blend_alpha = 0.0

    def _invalidate_dynamic_gpu_cache(self) -> None:
        self._dynamic_layer_cpu = None
        self._dynamic_mask_cpu = None
        self._dynamic_layer_u = None
        self._dynamic_mask_u = None
        self._dynamic_x = 0
        self._dynamic_y = 0

    def _should_use_gpu_path(self, roi_pixels: int | None = None) -> bool:
        if self._gpu_mode == "off":
            return False
        available, enabled = _opencl_runtime_state()
        if not (available and enabled):
            return False
        if self._gpu_mode == "on":
            return True
        if roi_pixels is None:
            return True
        return int(roi_pixels) >= OVERLAY_GPU_MIN_PIXELS

    def _should_use_gpu_for_roi(self, roi_w: int, roi_h: int) -> bool:
        pixels = max(0, int(roi_w)) * max(0, int(roi_h))
        return self._should_use_gpu_path(pixels)

    def pipeline_mode(self) -> str:
        available, enabled = _opencl_runtime_state()
        gpu_active = self._should_use_gpu_path()
        if gpu_active:
            return (
                f"Hybrid GPU (mode={self._gpu_mode} requested='{self._gpu_mode_raw}' "
                f"OpenCL available={available} enabled={enabled} min_pixels={OVERLAY_GPU_MIN_PIXELS}; "
                "ROI overlay compositing on UMat when large enough, rasterization on CPU)"
            )
        return (
            f"CPU (mode={self._gpu_mode} requested='{self._gpu_mode_raw}' "
            f"OpenCL available={available} enabled={enabled} min_pixels={OVERLAY_GPU_MIN_PIXELS})"
        )

    def _resolve_ops_for_frame(
        self,
        ops: list[tuple[np.ndarray, np.ndarray, int, int, float]],
        frame_w: int,
        frame_h: int,
        apply_independent_clamp: bool,
    ) -> list[tuple[np.ndarray, np.ndarray, int, int, float]]:
        resolved: list[tuple[np.ndarray, np.ndarray, int, int, float]] = []
        for sprite, sprite_mask, x, y, alpha in ops:
            ox, oy = int(x), int(y)
            if apply_independent_clamp:
                ox, oy = _clamp_op_origin(ox, oy, sprite.shape[1], sprite.shape[0], frame_w, frame_h)
            sh, sw = sprite.shape[:2]
            if sh <= 0 or sw <= 0:
                continue
            if ox >= frame_w or oy >= frame_h or (ox + sw) <= 0 or (oy + sh) <= 0:
                continue
            resolved.append((sprite, sprite_mask, ox, oy, alpha))
        return resolved

    def _compose_opaque_roi_cache(
        self,
        ops: list[tuple[np.ndarray, np.ndarray, int, int, float]],
        frame_w: int,
        frame_h: int,
        apply_independent_clamp: bool,
    ) -> tuple[np.ndarray, np.ndarray, int, int] | None:
        resolved = self._resolve_ops_for_frame(ops, frame_w, frame_h, apply_independent_clamp)
        if not resolved:
            return None

        min_x = frame_w
        min_y = frame_h
        max_x = -1
        max_y = -1
        for sprite, _sprite_mask, ox, oy, _alpha in resolved:
            sh, sw = sprite.shape[:2]
            x1 = max(0, ox)
            y1 = max(0, oy)
            x2 = min(frame_w, ox + sw)
            y2 = min(frame_h, oy + sh)
            if x1 >= x2 or y1 >= y2:
                continue
            min_x = min(min_x, x1)
            min_y = min(min_y, y1)
            max_x = max(max_x, x2 - 1)
            max_y = max(max_y, y2 - 1)
        if max_x < min_x or max_y < min_y:
            return None

        roi_w = max_x - min_x + 1
        roi_h = max_y - min_y + 1
        layer = np.zeros((roi_h, roi_w, 3), dtype=np.uint8)
        mask = np.zeros((roi_h, roi_w), dtype=np.uint8)
        wrote = False
        for sprite, sprite_mask, ox, oy, _alpha in resolved:
            wrote = _blit_opaque(layer, mask, sprite, sprite_mask, ox - min_x, oy - min_y) or wrote
        if not wrote or cv2.countNonZero(mask) == 0:
            return None
        return layer, mask, min_x, min_y

    def _overlay_layout(self, layouts: Dict, name: str) -> tuple[int, int, float]:
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

    def _build_transformed_op(
        self,
        sprite: np.ndarray,
        sprite_mask: np.ndarray,
        source_bbox: tuple[int, int, int, int],
        frame_w: int,
        frame_h: int,
        rot: int,
        flip_x: bool,
        flip_y: bool,
        transform_mat: np.ndarray,
        anchor_center: bool = False,
        alpha: float = 1.0,
    ) -> tuple[np.ndarray, np.ndarray, int, int, float] | None:
        if sprite.size == 0 or sprite_mask.size == 0:
            return None

        x1, y1, _, _ = source_bbox
        if rot or flip_x or flip_y:
            sprite = _apply_orientation(sprite, rot, flip_x, flip_y)
            sprite_mask = _apply_orientation(sprite_mask, rot, flip_x, flip_y)
            min_x, min_y, max_x, max_y = _transform_bbox(transform_mat, source_bbox)
            tx1 = int(np.floor(min_x))
            ty1 = int(np.floor(min_y))
            tx2 = int(np.ceil(max_x))
            ty2 = int(np.ceil(max_y))
            dest_w = max(1, tx2 - tx1 + 1)
            dest_h = max(1, ty2 - ty1 + 1)
            if sprite.shape[1] != dest_w or sprite.shape[0] != dest_h:
                sprite = cv2.resize(sprite, (dest_w, dest_h), interpolation=cv2.INTER_LINEAR)
                sprite_mask = cv2.resize(sprite_mask, (dest_w, dest_h), interpolation=cv2.INTER_NEAREST)
            # Remove transparent padding introduced by rotate/resize so clamp uses visible bounds.
            cropped = _crop_to_mask(sprite, sprite_mask)
            if cropped is not None:
                sprite, sprite_mask, (cx1, cy1, _, _) = cropped
                tx1 += cx1
                ty1 += cy1

            if anchor_center:
                src_cx = 0.5 * float(source_bbox[0] + source_bbox[2])
                src_cy = 0.5 * float(source_bbox[1] + source_bbox[3])
                t_cx = 0.5 * float(min_x + max_x)
                t_cy = 0.5 * float(min_y + max_y)
                target_min_x = float(min_x + (src_cx - t_cx))
                target_min_y = float(min_y + (src_cy - t_cy))
            else:
                # Re-anchor transformed overlay to its original top-left as closely as bounds allow.
                target_min_x = float(x1)
                target_min_y = float(y1)

            dx = _anchored_clamped_shift(min_x, max_x, target_min_x, float(max(0, frame_w - 1)))
            dy = _anchored_clamped_shift(min_y, max_y, target_min_y, float(max(0, frame_h - 1)))
            tx1 += int(round(dx))
            ty1 += int(round(dy))
            return sprite, sprite_mask, tx1, ty1, alpha

        return sprite, sprite_mask, x1, y1, alpha

    def _build_centerline_sprite(
        self,
        frame_w: int,
        frame_h: int,
        layout: tuple[int, int, float],
        scale: float,
        line_step: int,
    ) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int, int]] | None:
        x_off, _y_off, s_off = layout
        if frame_w <= 0 or frame_h <= 0:
            return None
        center_x = max(0, min(frame_w - 1, (frame_w // 2) + x_off))
        dash = max(10, int(line_step * 0.9 * s_off))
        gap = max(8, int(line_step * 0.7 * s_off))
        white_thick = max(2, int(scale * 3.2 * s_off))
        black_thick = max(1, int(scale * 1.8 * s_off))

        x1 = max(0, center_x - white_thick - 2)
        x2 = min(frame_w - 1, center_x + white_thick + 2)
        if x2 < x1:
            return None

        width = x2 - x1 + 1
        sprite = np.zeros((frame_h, width, 3), dtype=np.uint8)
        sprite_mask = np.zeros((frame_h, width), dtype=np.uint8)
        local_x = center_x - x1
        cycle = max(1, dash + gap)
        y = 0
        while y < frame_h:
            y2 = min(frame_h - 1, y + dash)
            cv2.line(sprite, (local_x, y), (local_x, y2), (255, 255, 255), white_thick, cv2.LINE_8)
            cv2.line(sprite, (local_x, y), (local_x, y2), (0, 0, 0), black_thick, cv2.LINE_8)
            cv2.line(sprite_mask, (local_x, y), (local_x, y2), 255, white_thick, cv2.LINE_8)
            y += cycle
        cropped = _crop_to_mask(sprite, sprite_mask)
        if cropped is None:
            return None
        sprite_c, mask_c, (lx1, ly1, lx2, ly2) = cropped
        return sprite_c, mask_c, (x1 + lx1, ly1, x1 + lx2, ly2)

    def _build_burst_sprite(
        self,
        frame_w: int,
        frame_h: int,
        layout: tuple[int, int, float],
    ) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int, int]] | None:
        x_off, y_off, s_off = layout
        if frame_w <= 0 or frame_h <= 0:
            return None
        rad = max(6, int(min(frame_h, frame_w) * 0.18 * s_off))
        c1 = (
            max(0, min(frame_w - 1, int(frame_w * 0.25) + x_off)),
            max(0, min(frame_h - 1, int(frame_h * 0.25) + y_off)),
        )
        c2 = (
            max(0, min(frame_w - 1, int(frame_w * 0.75) + x_off)),
            max(0, min(frame_h - 1, int(frame_h * 0.75) + y_off)),
        )

        x1 = max(0, min(c1[0] - rad, c2[0] - rad))
        y1 = max(0, min(c1[1] - rad, c2[1] - rad))
        x2 = min(frame_w - 1, max(c1[0] + rad, c2[0] + rad))
        y2 = min(frame_h - 1, max(c1[1] + rad, c2[1] + rad))
        if x2 < x1 or y2 < y1:
            return None

        width = x2 - x1 + 1
        height = y2 - y1 + 1
        sprite = np.zeros((height, width, 3), dtype=np.uint8)
        sprite_mask = np.zeros((height, width), dtype=np.uint8)
        c1_local = (c1[0] - x1, c1[1] - y1)
        c2_local = (c2[0] - x1, c2[1] - y1)
        cv2.circle(sprite, c1_local, rad, (0, 128, 255), -1, cv2.LINE_8)
        cv2.circle(sprite, c2_local, rad, (255, 64, 128), -1, cv2.LINE_8)
        cv2.circle(sprite_mask, c1_local, rad, 255, -1, cv2.LINE_8)
        cv2.circle(sprite_mask, c2_local, rad, 255, -1, cv2.LINE_8)
        return sprite, sprite_mask, (x1, y1, x2, y2)

    def _build_battle_score_sprite(
        self,
        frame_w: int,
        frame_h: int,
        layout: tuple[int, int, float],
        battle_entries: tuple[tuple[str, int], ...],
        scale: float,
        thick: int,
        allow_out_of_bounds: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int, int]] | None:
        if not battle_entries:
            return None

        x_off, y_off, s_off = layout
        bs_scale = max(0.25, scale * s_off)
        bs_thick = max(1, int(thick * s_off))
        y = int(50 * scale) + y_off
        x = 40 + x_off
        if not allow_out_of_bounds:
            y = max(16, y)
            x = max(8, x)
        step = max(14, int(36 * scale * s_off))

        layer = np.zeros((frame_h, frame_w, 3), dtype=np.uint8)
        mask = np.zeros((frame_h, frame_w), dtype=np.uint8)
        for name, win_count in battle_entries:
            text = f"{name}: {win_count} wins"
            cv2.putText(layer, text, (x, y), self._font, bs_scale, (0, 0, 0), bs_thick + 1, cv2.LINE_8)
            cv2.putText(layer, text, (x, y), self._font, bs_scale, (255, 255, 255), bs_thick, cv2.LINE_8)
            cv2.putText(mask, text, (x, y), self._font, bs_scale, 255, bs_thick + 1, cv2.LINE_8)
            y += step
        return _crop_to_mask(layer, mask)

    def _normalize_likes_payload(self, likes_overlay: Dict | None) -> Dict:
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
            "goal_text": goal_text,
            "goal_value": goal_value,
            "progress": progress,
            "prize_text": prize_text,
            "likes_line": likes_line,
        }

    def _build_likes_sprite(
        self,
        frame_w: int,
        frame_h: int,
        layout: tuple[int, int, float],
        likes_norm: Dict,
        scale: float,
        thick: int,
        allow_out_of_bounds: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int, int]] | None:
        x_off, y_off, s_off = layout
        likes_line = str(likes_norm.get("likes_line") or "").strip()
        if not likes_line:
            return None

        goal_value = int(likes_norm.get("goal_value", 0) or 0)
        progress = float(likes_norm.get("progress", 0.0) or 0.0)

        layer = np.zeros((frame_h, frame_w, 3), dtype=np.uint8)
        mask = np.zeros((frame_h, frame_w), dtype=np.uint8)

        max_text_width = max(120, int(frame_w - 40))
        text_scale = max(0.3, scale * s_off)
        text_thick = max(1, int(thick * s_off))
        (text_w, text_h), _ = cv2.getTextSize(likes_line, self._font, text_scale, text_thick)
        if text_w > max_text_width:
            shrink_ratio = max_text_width / float(max(1, text_w))
            text_scale = max(0.25, text_scale * shrink_ratio)
            (text_w, text_h), _ = cv2.getTextSize(likes_line, self._font, text_scale, text_thick)

        text_x = int((frame_w - text_w) / 2) + x_off
        text_y = int(54 * scale) + y_off
        if not allow_out_of_bounds:
            text_x = max(8, min(frame_w - text_w - 8, text_x))
            text_y = max(text_h + 18, text_y)
            text_y = min(frame_h - 8, text_y)

        # Two-pass headline to reduce CPU while preserving readability.
        cv2.putText(layer, likes_line, (text_x, text_y), self._font, text_scale, (0, 0, 0), text_thick + 2, cv2.LINE_AA)
        cv2.putText(layer, likes_line, (text_x, text_y), self._font, text_scale, (255, 255, 255), text_thick, cv2.LINE_AA)
        cv2.putText(mask, likes_line, (text_x, text_y), self._font, text_scale, 255, text_thick + 2, cv2.LINE_AA)

        if goal_value > 0:
            def _draw_pill(img: np.ndarray, x1: int, y1: int, x2: int, y2: int, color, thickness: int = -1) -> None:
                if x2 <= x1 or y2 <= y1:
                    return
                radius = max(1, int((y2 - y1) / 2))
                if (x2 - x1) <= radius * 2:
                    cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness, lineType=cv2.LINE_8)
                    return
                cv2.rectangle(img, (x1 + radius, y1), (x2 - radius, y2), color, thickness, lineType=cv2.LINE_8)
                cv2.circle(img, (x1 + radius, y1 + radius), radius, color, thickness, lineType=cv2.LINE_8)
                cv2.circle(img, (x2 - radius, y1 + radius), radius, color, thickness, lineType=cv2.LINE_8)

            bar_h = max(14, int(20 * scale * s_off))
            bar_y1 = text_y + max(8, int(12 * scale * s_off))
            bar_y2 = bar_y1 + bar_h
            bar_w = min(
                max(180, int((text_w + int(42 * text_scale)) * max(0.75, s_off))),
                max(180, int(frame_w * 0.72)),
            )
            bar_x1 = int((frame_w - bar_w) / 2) + x_off
            bar_x2 = bar_x1 + bar_w
            if not allow_out_of_bounds:
                bar_x1 = max(8, min(frame_w - bar_w - 8, bar_x1))
                bar_x2 = min(frame_w - 8, bar_x1 + bar_w)

            _draw_pill(layer, bar_x1, bar_y1, bar_x2, bar_y2, (30, 41, 59), -1)
            _draw_pill(mask, bar_x1, bar_y1, bar_x2, bar_y2, 255, -1)

            fill_w = int((bar_x2 - bar_x1) * progress)
            if fill_w > 0:
                fill_x2 = min(bar_x2, bar_x1 + fill_w)
                _draw_pill(layer, bar_x1, bar_y1, fill_x2, bar_y2, (132, 185, 255), -1)
                _draw_pill(mask, bar_x1, bar_y1, fill_x2, bar_y2, 255, -1)
                highlight_y = bar_y1 + max(1, int(bar_h * 0.3))
                cv2.line(
                    layer,
                    (bar_x1 + 2, highlight_y),
                    (max(bar_x1 + 2, fill_x2 - 2), highlight_y),
                    (255, 238, 248),
                    max(1, text_thick - 1),
                    cv2.LINE_8,
                )
                cv2.line(
                    mask,
                    (bar_x1 + 2, highlight_y),
                    (max(bar_x1 + 2, fill_x2 - 2), highlight_y),
                    255,
                    max(1, text_thick - 1),
                    cv2.LINE_8,
                )

            _draw_pill(layer, bar_x1, bar_y1, bar_x2, bar_y2, (128, 222, 255), max(1, text_thick - 1))
            _draw_pill(mask, bar_x1, bar_y1, bar_x2, bar_y2, 255, max(1, text_thick - 1))

            pct_text = f"{int(round(progress * 100.0))}%"
            pct_scale = max(0.32, text_scale * 0.72)
            (pct_w, pct_h), _ = cv2.getTextSize(pct_text, self._font, pct_scale, max(1, text_thick - 1))
            pct_x = int((bar_x1 + bar_x2 - pct_w) / 2)
            pct_y = bar_y1 + int((bar_h + pct_h) / 2) - 2
            cv2.putText(layer, pct_text, (pct_x, pct_y), self._font, pct_scale, (0, 0, 0), max(1, text_thick), cv2.LINE_8)
            cv2.putText(layer, pct_text, (pct_x, pct_y), self._font, pct_scale, (255, 255, 255), max(1, text_thick - 1), cv2.LINE_8)
            cv2.putText(mask, pct_text, (pct_x, pct_y), self._font, pct_scale, 255, max(1, text_thick), cv2.LINE_8)

        return _crop_to_mask(layer, mask)

    def _build_static_layers(
        self,
        frame_w: int,
        frame_h: int,
        center_visible: bool,
        burst_visible: bool,
        center_layout: tuple[int, int, float],
        burst_layout: tuple[int, int, float],
        scale: float,
        line_step: int,
        rot: int,
        flip_x: bool,
        flip_y: bool,
        transform_mat: np.ndarray,
    ) -> None:
        self._center_ops = []
        self._burst_ops = []
        self._static_source_boxes = []

        if center_visible:
            center_sprite = self._build_centerline_sprite(frame_w, frame_h, center_layout, scale, line_step)
            if center_sprite is not None:
                sprite, sprite_mask, bbox = center_sprite
                self._static_source_boxes.append(bbox)
                op = self._build_transformed_op(
                    sprite,
                    sprite_mask,
                    bbox,
                    frame_w,
                    frame_h,
                    rot,
                    flip_x,
                    flip_y,
                    transform_mat,
                    anchor_center=True,
                    alpha=1.0,
                )
                if op is not None:
                    y_shift = int(center_layout[1])
                    self._center_ops.append((op[0], op[1], int(op[2]), int(op[3]) + y_shift, op[4]))

        if burst_visible:
            burst_sprite = self._build_burst_sprite(frame_w, frame_h, burst_layout)
            if burst_sprite is not None:
                sprite, sprite_mask, bbox = burst_sprite
                self._static_source_boxes.append(bbox)
                op = self._build_transformed_op(
                    sprite,
                    sprite_mask,
                    bbox,
                    frame_w,
                    frame_h,
                    rot,
                    flip_x,
                    flip_y,
                    transform_mat,
                    alpha=0.1,
                )
                if op is not None:
                    self._burst_ops.append(op)

    def _build_dynamic_layers(
        self,
        frame_w: int,
        frame_h: int,
        battle_visible: bool,
        likes_visible: bool,
        battle_layout: tuple[int, int, float],
        likes_layout: tuple[int, int, float],
        battle_entries: tuple[tuple[str, int], ...],
        likes_norm: Dict,
        scale: float,
        thick: int,
        rot: int,
        flip_x: bool,
        flip_y: bool,
        transform_mat: np.ndarray,
    ) -> None:
        self._dynamic_ops = []
        self._dynamic_source_boxes = []
        allow_out_of_bounds = bool(rot or flip_x or flip_y)

        if battle_visible:
            battle_sprite = self._build_battle_score_sprite(
                frame_w,
                frame_h,
                battle_layout,
                battle_entries,
                scale,
                thick,
                allow_out_of_bounds=allow_out_of_bounds,
            )
            if battle_sprite is not None:
                sprite, sprite_mask, bbox = battle_sprite
                self._dynamic_source_boxes.append(bbox)
                op = self._build_transformed_op(
                    sprite,
                    sprite_mask,
                    bbox,
                    frame_w,
                    frame_h,
                    rot,
                    flip_x,
                    flip_y,
                    transform_mat,
                    alpha=1.0,
                )
                if op is not None:
                    self._dynamic_ops.append(op)

        if likes_visible:
            likes_sprite = self._build_likes_sprite(
                frame_w,
                frame_h,
                likes_layout,
                likes_norm,
                scale,
                thick,
                allow_out_of_bounds=allow_out_of_bounds,
            )
            if likes_sprite is not None:
                sprite, sprite_mask, bbox = likes_sprite
                self._dynamic_source_boxes.append(bbox)
                op = self._build_transformed_op(
                    sprite,
                    sprite_mask,
                    bbox,
                    frame_w,
                    frame_h,
                    rot,
                    flip_x,
                    flip_y,
                    transform_mat,
                    alpha=1.0,
                )
                if op is not None:
                    self._dynamic_ops.append(op)

    def _compose_static_cached_layers(
        self,
        frame_w: int,
        frame_h: int,
        apply_independent_clamp: bool,
    ) -> None:
        self._invalidate_static_gpu_cache()

        static_opaque = self._compose_opaque_roi_cache(
            self._center_ops,
            frame_w,
            frame_h,
            apply_independent_clamp,
        )
        if static_opaque is not None:
            (
                self._static_opaque_layer_cpu,
                self._static_opaque_mask_cpu,
                self._static_opaque_x,
                self._static_opaque_y,
            ) = static_opaque

        # Burst layer uses masked blending. We collapse to one cached ROI when safe.
        if len(self._burst_ops) == 1:
            alpha = float(self._burst_ops[0][4])
            if alpha > 0.0:
                static_blend = self._compose_opaque_roi_cache(
                    self._burst_ops,
                    frame_w,
                    frame_h,
                    apply_independent_clamp,
                )
                if static_blend is not None:
                    (
                        self._static_blend_layer_cpu,
                        self._static_blend_mask_cpu,
                        self._static_blend_x,
                        self._static_blend_y,
                    ) = static_blend
                    self._static_blend_alpha = alpha

    def _compose_dynamic_opaque_layer(
        self,
        frame_w: int,
        frame_h: int,
        apply_independent_clamp: bool,
    ) -> None:
        cached = self._compose_opaque_roi_cache(
            self._dynamic_ops,
            frame_w,
            frame_h,
            apply_independent_clamp,
        )
        if cached is not None:
            self._dynamic_layer_cpu, self._dynamic_mask_cpu, self._dynamic_x, self._dynamic_y = cached
        else:
            self._dynamic_layer_cpu = None
            self._dynamic_mask_cpu = None
            self._dynamic_x = 0
            self._dynamic_y = 0
        self._dynamic_layer_u = None
        self._dynamic_mask_u = None

    def render(self, frame: np.ndarray, state: Dict, likes_overlay: Dict | None = None) -> np.ndarray:
        frame_h, frame_w = frame.shape[:2]
        if frame_w <= 0 or frame_h <= 0:
            return frame

        overlays = state.get("overlay_states") or DEFAULT_OVERLAYS
        overlay_layouts = state.get("overlay_layouts") or {}
        center_layout = self._overlay_layout(overlay_layouts, "CenterDottedLine")
        burst_layout = self._overlay_layout(overlay_layouts, "BurstOverlay")
        battle_layout = self._overlay_layout(overlay_layouts, "BattleScore")
        likes_layout = self._overlay_layout(overlay_layouts, "TotalLikesOverlay")

        wins = state.get("win_counts") or {}
        enabled = set(state.get("enabled_dancers") or [])
        dancers = state.get("dancers") or []
        display_dancers = dancers if not enabled else [d for d in dancers if (d.get("name") or "") in enabled]
        battle_entries: list[tuple[str, int]] = []
        for dancer in display_dancers:
            name = str(dancer.get("name") or "Waiting")
            try:
                win_count = int(wins.get(name, 0))
            except Exception:
                win_count = 0
            battle_entries.append((name, win_count))
        battle_tuple = tuple(battle_entries)

        likes_norm = self._normalize_likes_payload(likes_overlay)
        likes_key = (
            int(likes_norm.get("total_likes", 0)),
            int(likes_norm.get("goal_value", 0)),
            str(likes_norm.get("goal_text", "")),
            str(likes_norm.get("prize_text", "")),
            round(float(likes_norm.get("progress", 0.0)), 5),
        )

        rot, flip_x, flip_y = _normalize_overlay_transform(state)
        transform_mat = _overlay_transform_matrix(frame_w, frame_h, rot, flip_x, flip_y)
        apply_independent_clamp = bool(rot or flip_x or flip_y)

        base_h = max(1, frame_h)
        scale = max(0.7, (base_h / 720.0) * 0.9)
        thick = max(1, int(scale * 2))
        line_step = max(20, int(base_h / 38))

        center_visible = bool(overlays.get("CenterDottedLine", False))
        burst_visible = bool(overlays.get("BurstOverlay", True))
        battle_visible = bool(overlays.get("BattleScore", True))
        likes_visible = bool(overlays.get("TotalLikesOverlay", True))

        static_key = (
            frame_w,
            frame_h,
            rot,
            flip_x,
            flip_y,
            center_visible,
            center_layout,
            burst_visible,
            burst_layout,
            line_step,
            round(scale, 4),
        )
        if static_key != self._static_key:
            self._build_static_layers(
                frame_w,
                frame_h,
                center_visible,
                burst_visible,
                center_layout,
                burst_layout,
                scale,
                line_step,
                rot,
                flip_x,
                flip_y,
                transform_mat,
            )
            self._compose_static_cached_layers(frame_w, frame_h, apply_independent_clamp)
            self._static_key = static_key

        dynamic_key = (
            frame_w,
            frame_h,
            rot,
            flip_x,
            flip_y,
            battle_visible,
            battle_layout,
            battle_tuple,
            likes_visible,
            likes_layout,
            likes_key,
            thick,
            round(scale, 4),
        )
        if dynamic_key != self._dynamic_key:
            self._build_dynamic_layers(
                frame_w,
                frame_h,
                battle_visible,
                likes_visible,
                battle_layout,
                likes_layout,
                battle_tuple,
                likes_norm,
                scale,
                thick,
                rot,
                flip_x,
                flip_y,
                transform_mat,
            )
            self._dynamic_key = dynamic_key
            self._compose_dynamic_opaque_layer(frame_w, frame_h, apply_independent_clamp)

        use_gpu = self._should_use_gpu_path()
        out = frame.copy()

        if self._static_opaque_layer_cpu is not None and self._static_opaque_mask_cpu is not None:
            static_h, static_w = self._static_opaque_layer_cpu.shape[:2]
            x1 = self._static_opaque_x
            y1 = self._static_opaque_y
            x2 = x1 + static_w
            y2 = y1 + static_h
            out_roi = out[y1:y2, x1:x2]
            if use_gpu and self._should_use_gpu_for_roi(static_w, static_h):
                if self._static_opaque_layer_u is None or self._static_opaque_mask_u is None:
                    self._static_opaque_layer_u = cv2.UMat(self._static_opaque_layer_cpu)
                    self._static_opaque_mask_u = cv2.UMat(self._static_opaque_mask_cpu)
                out_roi_u = cv2.UMat(out_roi)
                cv2.copyTo(self._static_opaque_layer_u, self._static_opaque_mask_u, out_roi_u)
                out[y1:y2, x1:x2] = out_roi_u.get()
            else:
                cv2.copyTo(self._static_opaque_layer_cpu, self._static_opaque_mask_cpu, out_roi)
        else:
            for sprite, sprite_mask, x, y, _ in self._center_ops:
                ox, oy = int(x), int(y)
                if apply_independent_clamp:
                    ox, oy = _clamp_op_origin(ox, oy, sprite.shape[1], sprite.shape[0], frame_w, frame_h)
                _blit_opaque(out, None, sprite, sprite_mask, ox, oy)

        if (
            self._static_blend_layer_cpu is not None
            and self._static_blend_mask_cpu is not None
            and self._static_blend_alpha > 0.0
        ):
            blend_h, blend_w = self._static_blend_layer_cpu.shape[:2]
            x1 = self._static_blend_x
            y1 = self._static_blend_y
            x2 = x1 + blend_w
            y2 = y1 + blend_h
            dst_roi = out[y1:y2, x1:x2]
            blended = cv2.addWeighted(
                dst_roi,
                1.0 - self._static_blend_alpha,
                self._static_blend_layer_cpu,
                self._static_blend_alpha,
                0.0,
            )
            cv2.copyTo(blended, self._static_blend_mask_cpu, dst_roi)
        else:
            for sprite, sprite_mask, x, y, alpha in self._burst_ops:
                ox, oy = int(x), int(y)
                if apply_independent_clamp:
                    ox, oy = _clamp_op_origin(ox, oy, sprite.shape[1], sprite.shape[0], frame_w, frame_h)
                _blend_masked(out, sprite, sprite_mask, ox, oy, alpha)

        if self._dynamic_layer_cpu is not None and self._dynamic_mask_cpu is not None:
            dyn_h, dyn_w = self._dynamic_layer_cpu.shape[:2]
            x1 = self._dynamic_x
            y1 = self._dynamic_y
            x2 = x1 + dyn_w
            y2 = y1 + dyn_h
            out_roi = out[y1:y2, x1:x2]
            if use_gpu and self._should_use_gpu_for_roi(dyn_w, dyn_h):
                if self._dynamic_layer_u is None or self._dynamic_mask_u is None:
                    self._dynamic_layer_u = cv2.UMat(self._dynamic_layer_cpu)
                    self._dynamic_mask_u = cv2.UMat(self._dynamic_mask_cpu)
                out_roi_u = cv2.UMat(out_roi)
                cv2.copyTo(self._dynamic_layer_u, self._dynamic_mask_u, out_roi_u)
                out[y1:y2, x1:x2] = out_roi_u.get()
            else:
                cv2.copyTo(self._dynamic_layer_cpu, self._dynamic_mask_cpu, out_roi)
        else:
            for sprite, sprite_mask, x, y, _ in self._dynamic_ops:
                ox, oy = int(x), int(y)
                if apply_independent_clamp:
                    ox, oy = _clamp_op_origin(ox, oy, sprite.shape[1], sprite.shape[0], frame_w, frame_h)
                _blit_opaque(out, None, sprite, sprite_mask, ox, oy)

        return out


_renderer = OverlayRenderer()


def get_render_pipeline_mode() -> str:
    return _renderer.pipeline_mode()


def draw_overlay(frame: np.ndarray, state: Dict, likes_overlay: Dict | None = None) -> np.ndarray:
    return _renderer.render(frame, state, likes_overlay)

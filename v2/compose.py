"""dirty-tile RGBA compositor — the "do less work" payoff.

keeps a persistent full-resolution premultiplied-RGBA buffer. each frame, only the tiles the
codec flagged as changed (motion present, from MVs) or whose alpha shifted (persistence
fade) are re-premultiplied and rewritten; every other tile keeps last frame's bytes. a fully
static scene produces *zero* dirty tiles → no compositing and no repaint at all.

the player wraps this buffer in a single QImage (built once) and repaints only the returned
dirty rects, so the full-frame QImage.copy() that cost v1 ~5 ms/frame is gone too.

threading: the worker composites under `lock`; the GUI paints under the same `lock`.
"""

from __future__ import annotations

import threading

import cv2
import numpy as np

from . import gpu


def _pool_max(a: np.ndarray, th: int, tw: int) -> np.ndarray:
    """Max-pool a 2D uint8 array into (ceil(H/th), ceil(W/tw)) tiles."""
    h, w = a.shape
    Th, Tw = (h + th - 1) // th, (w + tw - 1) // tw
    pad_h, pad_w = Th * th - h, Tw * tw - w
    if pad_h or pad_w:
        a = np.pad(a, ((0, pad_h), (0, pad_w)))
    return a.reshape(Th, th, Tw, tw).max(axis=(1, 3))


class Compositor:
    ALPHA_EPS = 6     # alpha delta (0..255) below which a tile is unchanged
    VIS_FLOOR = 32    # a tile must reach at least this alpha to be worth repainting for motion

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.buffer: np.ndarray | None = None     # (H, W, 4) uint8, RGBA premultiplied
        self.width = 0
        self.height = 0
        self._prev_alpha_grid: np.ndarray | None = None  # (gh, gw) int16 floored alpha, last frame
        self._generation = 0                       # bumps whenever the buffer is reallocated

    @property
    def generation(self) -> int:
        return self._generation

    def ensure_size(self, w: int, h: int) -> None:
        if self.buffer is not None and self.width == w and self.height == h:
            return
        self.buffer = np.zeros((h, w, 4), dtype=np.uint8)
        self.width, self.height = w, h
        self._prev_alpha_grid = None               # force a full redraw on the next frame
        self._generation += 1

    def composite(
        self,
        rgb: np.ndarray,        # (H, W, 3) uint8 RGB
        mask_grid: np.ndarray,  # (gh, gw) uint8 — shaped mask, pre-floor, pre-upscale
        motion_grid: np.ndarray,  # (gh, gw) uint8 — thresholded motion (content changed)
        tile_px: int,
        alpha_floor: float,
        use_gpu: bool = False,
    ) -> list[tuple[int, int, int, int]]:
        """Composite only the tiles that visibly changed; return their (x, y, w, h) rects.

        change detection runs entirely at macroblock-grid resolution (tiny), so a fully-static
        frame skips the full-res upscale/premultiply altogether. a tile is dirty when:
          • its alpha shifted (persistence fade in/out), OR
          • it has real motion AND is actually visible (above VIS_FLOOR alpha).
        a near-transparent background ghost is never refreshed — you can't see it anyway.
        """
        h, w = rgb.shape[:2]
        gh, gw = mask_grid.shape
        self.ensure_size(w, h)
        assert self.buffer is not None
        tp = max(16, int(tile_px))

        # floored alpha at grid resolution (for visibility + delta detection)
        floor_u8 = max(0, min(255, int(alpha_floor * 255)))
        alpha_grid = cv2.convertScaleAbs(mask_grid, alpha=(255 - floor_u8) / 255.0, beta=floor_u8)

        if self._prev_alpha_grid is None or self._prev_alpha_grid.shape != alpha_grid.shape:
            self._prev_alpha_grid = np.full_like(alpha_grid, -1, dtype=np.int16)

        block_f = max(1, h // gh)             # pixels per grid cell
        pool = max(1, tp // block_f)          # grid cells per tile
        motion_t = _pool_max(motion_grid, pool, pool) > 0
        visible_t = _pool_max(alpha_grid, pool, pool) > self.VIS_FLOOR
        gdelta = np.abs(alpha_grid.astype(np.int16) - self._prev_alpha_grid).astype(np.uint8)
        delta_t = _pool_max(gdelta, pool, pool) > self.ALPHA_EPS
        dirty = (motion_t & visible_t) | delta_t
        self._prev_alpha_grid = alpha_grid.astype(np.int16)

        ys_idx, xs_idx = np.nonzero(dirty)
        if len(ys_idx) == 0:
            return []

        # only now pay for full-res work: upscale mask, apply floor, premultiply once.
        # on GPU (CuPy) when enabled+available, else the CPU path.
        if use_gpu and gpu.available():
            premul, alpha_full = gpu.composite_full(rgb, mask_grid, floor_u8)
        else:
            mask_full = cv2.resize(mask_grid, (w, h), interpolation=cv2.INTER_LINEAR)
            alpha_full = cv2.convertScaleAbs(mask_full, alpha=(255 - floor_u8) / 255.0, beta=floor_u8)
            a3 = cv2.merge([alpha_full, alpha_full, alpha_full])
            premul = cv2.multiply(rgb, a3, scale=1.0 / 255.0)

        buf = self.buffer
        rects: list[tuple[int, int, int, int]] = []
        for ty, tx in zip(ys_idx.tolist(), xs_idx.tolist()):
            ys, xs = ty * tp, tx * tp
            ye, xe = min(h, ys + tp), min(w, xs + tp)
            buf[ys:ye, xs:xe, 0:3] = premul[ys:ye, xs:xe]
            buf[ys:ye, xs:xe, 3] = alpha_full[ys:ye, xs:xe]
            rects.append((xs, ys, xe - xs, ye - ys))
        return rects

    def blit_full(self, rgba: np.ndarray) -> list[tuple[int, int, int, int]]:
        """Overwrite the whole buffer (used by debug views) and force a full repaint."""
        h, w = rgba.shape[:2]
        self.ensure_size(w, h)
        assert self.buffer is not None
        self.buffer[:] = rgba
        self._prev_alpha_grid = None   # invalidate so the next normal frame redraws everything
        return [(0, 0, w, h)]

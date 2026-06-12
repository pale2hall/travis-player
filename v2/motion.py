"""turn the codec's data into a coarse activity field — no full-frame pixel work.

the heart of v2: instead of diffing the whole frame, scatter each motion vector's magnitude
into a macroblock grid. blocks the encoder didn't move (skip-coded → no MV) stay zero. that
grid *is* the raw motion mask, computed for free as a byproduct of decode.

a pixel-diff fallback is kept for I-frames (no MVs) and the diff engine, so v2 degrades
gracefully instead of breaking on MV-less content.
"""

from __future__ import annotations

import cv2
import numpy as np

from .decode import DecodedFrame


def grid_dims(width: int, height: int, block_px: int) -> tuple[int, int]:
    """(grid_w, grid_h) for the activity field at the given macroblock size."""
    b = max(2, int(block_px))
    return max(1, width // b), max(1, height // b)


def activity_from_mvs(
    mvs: np.ndarray,
    width: int,
    height: int,
    block_px: int,
    gain: float,
) -> np.ndarray:
    """Scatter MV magnitudes into a (gh, gw) uint8 activity grid.

    each MV's per-pixel motion magnitude (motion_x/y are in 1/motion_scale pel units) is
    deposited at its destination block via a running max, then scaled by `gain` and clipped
    to 0..255 so the rest of the pipeline can reuse v1's uint8 mask math.
    """
    gw, gh = grid_dims(width, height, block_px)
    grid = np.zeros((gh, gw), dtype=np.float32)
    if mvs is None or len(mvs) == 0:
        return grid.astype(np.uint8)

    scale = np.maximum(1, mvs["motion_scale"].astype(np.float32))
    mag = np.hypot(mvs["motion_x"].astype(np.float32),
                   mvs["motion_y"].astype(np.float32)) / scale  # pixels of motion
    gx = np.clip(mvs["dst_x"].astype(np.int32) // block_px, 0, gw - 1)
    gy = np.clip(mvs["dst_y"].astype(np.int32) // block_px, 0, gh - 1)
    val = np.clip(mag * gain, 0, 255).astype(np.float32)

    # running max per cell (np.maximum.at handles duplicate destinations correctly)
    np.maximum.at(grid, (gy, gx), val)
    return grid.astype(np.uint8)


class DiffActivity:
    """v1-style pixel-diff fallback, emitted at the same grid resolution as the MV path
    so downstream code is engine-agnostic."""

    def __init__(self) -> None:
        self._prev: np.ndarray | None = None

    def reset(self) -> None:
        self._prev = None

    def compute(
        self,
        frame: DecodedFrame,
        block_px: int,
        threshold: float,
    ) -> np.ndarray:
        gw, gh = grid_dims(frame.width, frame.height, block_px)
        small = cv2.resize(frame.rgb, (gw, gh), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        if self._prev is None or self._prev.shape != gray.shape:
            self._prev = gray
            return np.zeros((gh, gw), dtype=np.uint8)
        diff = cv2.absdiff(gray, self._prev)
        self._prev = gray
        t = max(1, int(threshold * 255))
        lo = max(1, t // 2)
        hi = max(lo + 1, (t * 3) // 2)
        s = 255 // (hi - lo) if (hi - lo) > 0 else 255
        shifted = cv2.subtract(diff, np.array([lo], dtype=np.uint8))
        return cv2.convertScaleAbs(shifted, alpha=s, beta=0)

"""activity grid → feathered alpha grid.

this is v1's proven mask-shaping pipeline (scene-cut, min-region, persistence, padding,
push-full, feather, falloff) lifted almost verbatim — only the *source* changed (a motion-
vector activity grid instead of a full-frame pixel diff). because it runs at macroblock grid
resolution (~120×68 for 1080p) instead of proc-res, it's effectively free.
"""

from __future__ import annotations

import cv2
import numpy as np

from .params import Params


class MaskBuilder:
    """Holds the cross-frame state (persistence accumulator, falloff LUT) and turns a raw
    activity grid into a shaped alpha grid."""

    def __init__(self) -> None:
        self._activity: np.ndarray | None = None
        self._falloff_lut: tuple[float, np.ndarray] | None = None
        self.last_active_frac: float = 0.0

    def reset_persistence(self) -> None:
        self._activity = None

    def _grid_px(self, ref_px_1080: int, video_h: int, block_px: int) -> int:
        """Convert a 1080p-reference pixel value into grid cells at this block size."""
        if ref_px_1080 <= 0:
            return 0
        actual = ref_px_1080 * (video_h / 1080.0)
        return max(1, int(round(actual / max(2, block_px))))

    def build(
        self,
        activity: np.ndarray,      # (gh, gw) uint8 raw motion strength
        params: Params,
        frame_period: float,
        video_h: int,
    ) -> np.ndarray:
        p = params
        block = p.mv_block_px
        small_mask = activity

        # threshold floor: anything below counts as static
        t = max(0, min(255, int(p.threshold * 255)))
        if t > 0:
            _, small_mask = cv2.threshold(small_mask, t, 0, cv2.THRESH_TOZERO)

        # scene-cut rejection
        if p.scene_cut_ignore and small_mask.size > 0:
            frac = float(cv2.countNonZero(small_mask)) / small_mask.size
            if frac > p.scene_cut_thresh:
                small_mask = np.zeros_like(small_mask)

        # min region size: drop small noise blobs
        min_ext = self._grid_px(p.min_region_px_1080, video_h, block)
        if min_ext > 0 and small_mask.any():
            min_area = min_ext * min_ext
            _, bin_mask = cv2.threshold(small_mask, 1, 255, cv2.THRESH_BINARY)
            n, labels, stats, _ = cv2.connectedComponentsWithStats(bin_mask, connectivity=8)
            keep = np.zeros(n, dtype=np.uint8)
            for i in range(1, n):
                if stats[i, cv2.CC_STAT_AREA] >= min_area:
                    keep[i] = 1
            small_mask = cv2.bitwise_and(small_mask, keep[labels].astype(np.uint8) * 255)

        # persistence: running max with linear decay
        persist = max(0.0, min(5.0, p.persist_seconds))
        if persist > 0.0:
            if self._activity is None or self._activity.shape != small_mask.shape:
                self._activity = small_mask.copy()
            else:
                decay = min(255, max(1, int(255 * (frame_period / persist))))
                decayed = cv2.subtract(self._activity, np.array([decay], dtype=np.uint8))
                self._activity = cv2.max(decayed, small_mask)
            small_mask = self._activity
        else:
            self._activity = None

        # padding: dilate outward
        pad = self._grid_px(p.padding_px_1080, video_h, block)
        if pad > 0:
            k = 2 * pad + 1
            small_mask = cv2.dilate(small_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))

        # push to full: snap above-threshold pixels to opaque before feathering
        if p.push_full > 0:
            thresh_u8 = max(0, int(p.push_full * 2.55) - 1)
            _, small_mask = cv2.threshold(small_mask, thresh_u8, 255, cv2.THRESH_BINARY)

        # feather: gaussian blur for soft edges
        feather = self._grid_px(p.feather_px_1080, video_h, block)
        r = max(1, feather) | 1
        small_mask = cv2.GaussianBlur(small_mask, (r, r), 0)

        # falloff curve (power LUT)
        falloff = max(0.1, min(5.0, p.feather_falloff))
        if abs(falloff - 1.0) > 0.01:
            if self._falloff_lut is None or self._falloff_lut[0] != falloff:
                xs = np.arange(256, dtype=np.float32) / 255.0
                lut = np.clip(np.power(xs, falloff) * 255.0, 0, 255).astype(np.uint8)
                self._falloff_lut = (falloff, lut)
            small_mask = cv2.LUT(small_mask, self._falloff_lut[1])

        if small_mask.size:
            self.last_active_frac = float(cv2.countNonZero(small_mask)) / small_mask.size
        return small_mask

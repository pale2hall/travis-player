"""frame worker — orchestrates decode → motion → mask → dirty-tile composite.

runs in a QThread. paces output against the MasterClock (so A/V stays locked and we drop
frames instead of drifting), selects the engine (MVs / pixel-diff / auto), and writes only
dirty tiles into the shared compositor buffer. emits the dirty-rect list to the player.
"""

from __future__ import annotations

import logging
import time

import cv2
import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from . import params as P
from .clock import MasterClock
from .compose import Compositor
from .decode import Decoder
from .mask import MaskBuilder
from .motion import DiffActivity, activity_from_mvs
from .params import Params

log = logging.getLogger("travis.v2")


class FrameWorker(QObject):
    frame_ready = pyqtSignal(object)            # list[(x,y,w,h)] dirty rects in video coords
    position_update = pyqtSignal(float, float)  # pos_ms, duration_ms
    stats_update = pyqtSignal(str)              # one-line status for the control panel
    stream_info = pyqtSignal(dict)              # codec/width/height/fps, emitted once on open
    finished = pyqtSignal()

    def __init__(self, source: str, params: Params, clock: MasterClock, compositor: Compositor) -> None:
        super().__init__()
        self.source = source
        self.params = params
        self.clock = clock
        self.compositor = compositor
        self._running = True
        self._seek_request: float | None = None
        self._decoder = Decoder(source)
        self._mask = MaskBuilder()
        self._diff = DiffActivity()
        self._last_activity: np.ndarray | None = None

    # ── thread-safe controls ──
    def request_seek(self, frac: float) -> None:
        self._seek_request = max(0.0, min(1.0, frac))

    def stop(self) -> None:
        self._running = False

    # ── main loop ──
    def run(self) -> None:
        try:
            self._decoder.open()
        except Exception:
            log.exception("decoder open failed: %s", self.source)
            self.finished.emit()
            return

        dur = self._decoder.duration
        frame_period = 1.0 / max(1.0, self._decoder.fps)
        self.clock.reset(0.0)
        self.stream_info.emit({
            "codec": self._decoder.codec,
            "width": self._decoder.width,
            "height": self._decoder.height,
            "fps": self._decoder.fps,
        })

        proc_times: list[float] = []
        dirty_counts: list[int] = []
        n_drop = 0
        stats_t = time.monotonic()

        while self._running:
            if self._seek_request is not None:
                frac = self._seek_request
                self._seek_request = None
                self._decoder.seek(frac)
                self._mask.reset_persistence()
                self._diff.reset()
                self.clock.reset(frac * dur)

            if self.params.paused:
                time.sleep(0.02)
                continue

            frame = self._decoder.read()
            if frame is None:  # EOF → loop
                self._decoder.rewind()
                self._mask.reset_persistence()
                self._diff.reset()
                self.clock.reset(0.0)
                continue

            # pace against the master clock
            delay = frame.pts - self.clock.now()
            if delay > 0.5:                      # far ahead (e.g. after a seek) → resync
                self.clock.reset(frame.pts)
            elif delay < -2 * frame_period:       # behind → drop to catch up
                n_drop += 1
                continue
            elif delay > 0:
                time.sleep(min(delay, frame_period))

            t0 = time.monotonic()
            rects = self._process(frame)
            proc_times.append(time.monotonic() - t0)
            dirty_counts.append(len(rects))

            if rects:
                self.frame_ready.emit(rects)
            if dur > 0:
                self.position_update.emit(frame.pts * 1000.0, dur * 1000.0)

            # perf stats every ~60 processed frames
            if len(proc_times) >= 60:
                avg = sum(proc_times) / len(proc_times) * 1000
                p95 = sorted(proc_times)[int(len(proc_times) * 0.95)] * 1000
                real_fps = 60 / max(1e-6, time.monotonic() - stats_t)
                avg_dirty = sum(dirty_counts) / len(dirty_counts)
                from . import gpu
                gpu_on = self.params.use_gpu and gpu.available()
                log.info("perf: proc=%.1fms avg / %.1fms p95  fps=%.1f  drop=%d  "
                         "dirty=%.0f tiles  active=%.0f%%  eng=%s  gpu=%s",
                         avg, p95, real_fps, n_drop, avg_dirty,
                         self._mask.last_active_frac * 100, self.params.engine,
                         "on" if gpu_on else "off")
                self.stats_update.emit(
                    f"proc {avg:.1f}ms · {real_fps:.0f}fps · {avg_dirty:.0f} dirty tiles · "
                    f"{self._mask.last_active_frac*100:.0f}% active · gpu {'on' if gpu_on else 'off'}"
                )
                proc_times.clear()
                dirty_counts.clear()
                n_drop = 0
                stats_t = time.monotonic()

        self._decoder.close()
        self.finished.emit()

    # ── per-frame pipeline ──
    def _process(self, frame) -> list[tuple[int, int, int, int]]:
        p = self.params
        w, h = frame.width, frame.height
        block = p.mv_block_px

        # 1. activity grid — from the codec's MVs, or pixel-diff fallback
        use_diff = (p.engine == P.ENGINE_DIFF) or (p.engine == P.ENGINE_AUTO and frame.mvs is None)
        if use_diff:
            activity = self._diff.compute(frame, block, p.threshold)
        elif frame.mvs is not None:
            activity = activity_from_mvs(frame.mvs, w, h, block, p.mv_gain)
        else:  # MV engine on an MV-less frame (I-frame)
            from .motion import grid_dims
            gw, gh = grid_dims(w, h, block)
            activity = np.zeros((gh, gw), dtype=np.uint8)
            if not p.iframe_hold:
                self._mask.reset_persistence()
        self._last_activity = activity

        # 2. shape into an alpha grid (v1's mask math, at grid res)
        frame_period = 1.0 / max(1.0, self._decoder.fps)
        mask_grid = self._mask.build(activity, p, frame_period, h)

        # 3. debug views short-circuit to a full blit
        if p.debug_mode != P.VIEW_NORMAL:
            rgba = self._debug_rgba(frame, mask_grid, activity, p.debug_mode)
            with self.compositor.lock:
                return self.compositor.blit_full(rgba)

        # 4. thresholded motion grid → which tiles' content actually changed
        #    (sub-threshold MV speckle must not flag the whole frame as dirty)
        thresh_u8 = max(1, int(p.threshold * 255))
        _, motion_grid = cv2.threshold(activity, thresh_u8, 255, cv2.THRESH_BINARY)

        # 5. composite only the visibly-changed tiles (upscale + premultiply happen inside,
        #    and are skipped entirely when nothing is dirty)
        with self.compositor.lock:
            return self.compositor.composite(
                frame.rgb, mask_grid, motion_grid, p.tile_px, p.alpha_floor, p.use_gpu)

    def _debug_rgba(self, frame, mask_grid, activity, mode) -> np.ndarray:
        w, h = frame.width, frame.height
        rgb = frame.rgb
        if mode == P.VIEW_MV:
            heat = cv2.applyColorMap(cv2.resize(activity, (w, h), interpolation=cv2.INTER_NEAREST),
                                     cv2.COLORMAP_INFERNO)
            heat = cv2.cvtColor(heat, cv2.COLOR_BGR2RGB)
            blended = cv2.addWeighted(rgb, 0.35, heat, 0.65, 0)
            return self._opaque(blended)
        if mode == P.VIEW_TILES:
            # dim the frame, outline tiles that would be dirty this frame
            dim = (rgb.astype(np.uint16) * 40 // 100).astype(np.uint8)
            tp = max(16, self.params.tile_px)
            act_full = cv2.resize(activity, (w, h), interpolation=cv2.INTER_NEAREST)
            for ys in range(0, h, tp):
                for xs in range(0, w, tp):
                    if act_full[ys:ys + tp, xs:xs + tp].any():
                        dim[ys:ys + tp, xs:xs + tp] = rgb[ys:ys + tp, xs:xs + tp]
                        cv2.rectangle(dim, (xs, ys), (min(w, xs + tp) - 1, min(h, ys + tp) - 1),
                                      (0, 255, 80), 1)
            return self._opaque(dim)
        if mode == P.VIEW_BINARY:
            full = cv2.resize(mask_grid, (w, h), interpolation=cv2.INTER_NEAREST)
            _, bw = cv2.threshold(full, 127, 255, cv2.THRESH_BINARY)
            return self._opaque(cv2.cvtColor(bw, cv2.COLOR_GRAY2RGB))
        # VIEW_OFF
        return self._opaque(rgb)

    @staticmethod
    def _opaque(rgb: np.ndarray) -> np.ndarray:
        h, w = rgb.shape[:2]
        rgba = np.empty((h, w, 4), dtype=np.uint8)
        rgba[..., 0:3] = rgb
        rgba[..., 3] = 255
        return rgba

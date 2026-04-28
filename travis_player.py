"""travis-player: motion-based transparency video player

Architecture:
  - cv2.VideoCapture decodes video frames in a worker thread
  - per-frame diff computed at proc_div'th resolution (default 1/8) → fast
  - alpha mask upscaled and applied to full-res RGB to build RGBA
  - PyQt6 frameless+translucent window displays composite at OS-level transparency
  - audio sidecar: parallel mpv process. wrapped in a Windows Job Object so it
    dies automatically when the python process exits (no orphan audio)
  - separate control panel window with sliders for live tuning

Run:
  .venv/Scripts/python travis_player.py "video.mp4"
"""

from __future__ import annotations

import atexit
import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import traceback
from ctypes import wintypes
from dataclasses import asdict, dataclass, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path

import cv2
import numpy as np


# ─── logging setup ──────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "travis.log"

log = logging.getLogger("travis")


def _setup_logging() -> None:
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    # catch unhandled exceptions so they hit the log file
    def _excepthook(exc_type, exc, tb):
        log.critical("UNCAUGHT EXCEPTION:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
    sys.excepthook = _excepthook
from PyQt6.QtCore import (
    QObject, QPoint, Qt, QThread, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDragEnterEvent, QDropEvent, QImage, QKeyEvent, QMouseEvent,
    QPainter, QPixmap, QPolygon,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget,
)


# ─── Windows job object: child processes die when parent dies ───────────────
_job_handle: int | None = None

def _create_job_for_children() -> int | None:
    """Create a Windows Job Object that auto-kills any child added to it
    when this process exits (even on hard crash). Returns the job handle."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit",     ctypes.c_int64),
            ("LimitFlags",              wintypes.DWORD),
            ("MinimumWorkingSetSize",   ctypes.c_size_t),
            ("MaximumWorkingSetSize",   ctypes.c_size_t),
            ("ActiveProcessLimit",      wintypes.DWORD),
            ("Affinity",                ctypes.c_size_t),
            ("PriorityClass",           wintypes.DWORD),
            ("SchedulingClass",         wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount",  ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount",   ctypes.c_uint64),
            ("WriteTransferCount",  ctypes.c_uint64),
            ("OtherTransferCount",  ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo",                IO_COUNTERS),
            ("ProcessMemoryLimit",    ctypes.c_size_t),
            ("JobMemoryLimit",        ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed",     ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    JobObjectExtendedLimitInformation = 9
    if not kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info),
    ):
        kernel32.CloseHandle(job)
        return None
    return job


# ─── audio controller: parallel mpv driven via IPC named pipe ───────────────
class AudioController:
    """Runs an audio-only mpv subprocess and controls it via Windows named pipe.
    The pipe lets us pause/resume mpv without restarting it (no audio resync)."""

    def __init__(self, source: str, pipe_name: str | None = None) -> None:
        self.source = source
        self.pipe_name = pipe_name or rf"\\.\pipe\travis-audio-{os.getpid()}"
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        mpv = shutil.which("mpv")
        if not mpv:
            log.warning("mpv not in PATH — running silent")
            return
        args = [
            mpv, "--no-video", "--no-terminal", "--really-quiet",
            "--keep-open=no", "--loop-file=inf",
            f"--input-ipc-server={self.pipe_name}",
            self.source,
        ]
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        in_job = _add_to_job(self._proc.pid)
        log.info("audio sidecar started pid=%d  pipe=%s  job=%s",
                 self._proc.pid, self.pipe_name, "yes" if in_job else "NO")

    def set_paused(self, paused: bool) -> None:
        """Send pause command via named pipe. Best-effort — quietly skip on failure."""
        if self._proc is None or self._proc.poll() is not None:
            return
        cmd = (
            '{"command":["set_property","pause",%s]}\n' % ("true" if paused else "false")
        ).encode("utf-8")
        if sys.platform != "win32":
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            GENERIC_WRITE = 0x40000000
            OPEN_EXISTING = 3
            handle = kernel32.CreateFileW(
                self.pipe_name, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None,
            )
            if handle == -1 or handle == 0:
                return
            try:
                written = wintypes.DWORD(0)
                kernel32.WriteFile(handle, cmd, len(cmd), ctypes.byref(written), None)
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            log.exception("audio pause IPC failed")

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            log.info("terminating audio sidecar pid=%d", self._proc.pid)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                log.warning("audio sidecar didn't terminate, killing")
                self._proc.kill()
        self._proc = None


def _add_to_job(pid: int) -> bool:
    if _job_handle is None or sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    ok = bool(kernel32.AssignProcessToJobObject(_job_handle, handle))
    kernel32.CloseHandle(handle)
    return ok


# ─── parameters (mutable at runtime, shared by GUI + worker) ────────────────
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"
PRESETS_DIR = Path(__file__).resolve().parent / "presets"

# fields excluded from persistence (transient runtime state)
_TRANSIENT = {"paused"}
# fields excluded from presets (presets are about the look, not window placement)
_PRESET_EXCLUDED = _TRANSIENT | {
    "controls_x", "controls_y", "controls_w", "controls_h",
    "player_x", "player_y", "player_w", "player_h",
}


def _safe_preset_name(name: str) -> str:
    import re as _re
    name = name.strip()
    name = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:64].strip()


def list_presets() -> list[str]:
    if not PRESETS_DIR.exists():
        return []
    return sorted(p.stem for p in PRESETS_DIR.glob("*.json"))


@dataclass
class Params:
    # --- visibility ---
    alpha_floor: float = 0.05         # min alpha for fully-static regions (0=invisible, 1=opaque)
    threshold: float = 0.04           # luma diff to count as motion (0..1, lower = more sensitive)

    # --- region shaping (all sizes in "pixels @ 1080p reference"; auto-scaled to actual res) ---
    padding_px_1080: int = 0          # outward dilation around active regions
    feather_px_1080: int = 8          # softness of mask edges
    feather_falloff: float = 1.0      # power curve on the feathered mask: <1 softer, =1 linear, >1 harder cutoff
    min_region_px_1080: int = 0       # minimum motion blob extent — smaller blobs treated as noise
    edge_sensitivity: float = 0.0     # 0=ignore edges, 1=only keep motion in textured areas

    # --- temporal ---
    persist_seconds: float = 0.5      # how long active regions stay visible after motion stops (0..5)
    scene_cut_ignore: bool = True     # drop frames where almost everything changed at once (cuts/flashes)
    scene_cut_thresh: float = 0.7     # fraction of "active" pixels that counts as a scene change

    # --- compute ---
    proc_div: int = 8                 # downscale factor for diff math (1=full res, higher = faster)

    # --- view ---
    debug_mode: int = 0               # 0=normal 1=heatmap 2=binary 3=off
    paused: bool = False              # transient — not saved

    # --- window geometry (auto-saved; -1 = use default) ---
    controls_x: int = -1
    controls_y: int = -1
    controls_w: int = 440
    controls_h: int = 720
    player_x: int = -1
    player_y: int = -1
    player_w: int = 960
    player_h: int = 540

    def save(self, path: Path = SETTINGS_FILE) -> None:
        try:
            data = {k: v for k, v in asdict(self).items() if k not in _TRANSIENT}
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            log.exception("failed to save settings")

    def load(self, path: Path = SETTINGS_FILE) -> None:
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            valid_names = {f.name for f in fields(self)}
            for k, v in data.items():
                if k in valid_names and k not in _TRANSIENT:
                    setattr(self, k, v)
            log.info("loaded settings from %s", path)
        except Exception:
            log.exception("failed to load settings — using defaults")

    def save_preset(self, name: str) -> Path | None:
        safe = _safe_preset_name(name)
        if not safe:
            return None
        PRESETS_DIR.mkdir(exist_ok=True)
        path = PRESETS_DIR / f"{safe}.json"
        try:
            data = {k: v for k, v in asdict(self).items() if k not in _PRESET_EXCLUDED}
            path.write_text(json.dumps(data, indent=2))
            log.info("saved preset '%s' -> %s", safe, path)
            return path
        except Exception:
            log.exception("failed to save preset '%s'", safe)
            return None

    def load_preset(self, name: str) -> bool:
        safe = _safe_preset_name(name)
        path = PRESETS_DIR / f"{safe}.json"
        if not path.exists():
            log.warning("preset not found: %s", path)
            return False
        try:
            data = json.loads(path.read_text())
            valid_names = {f.name for f in fields(self)}
            for k, v in data.items():
                if k in valid_names and k not in _PRESET_EXCLUDED:
                    setattr(self, k, v)
            log.info("loaded preset '%s'", safe)
            return True
        except Exception:
            log.exception("failed to load preset '%s'", safe)
            return False


# ─── frame producer thread ──────────────────────────────────────────────────
class FrameWorker(QObject):
    frame_ready = pyqtSignal(QImage)
    finished = pyqtSignal()

    def __init__(self, video_path: str, params: Params) -> None:
        super().__init__()
        self.video_path = video_path
        self.params = params
        self._running = True
        self._cap: cv2.VideoCapture | None = None
        self._prev_gray_small: np.ndarray | None = None
        self._activity: np.ndarray | None = None
        self._frame_period: float = 1.0 / 24.0
        self._video_h: int = 1080
        self._buf_owner: np.ndarray | None = None
        self._falloff_lut: tuple[float, np.ndarray] | None = None  # cached (falloff_value, lut)

    def _ref_to_proc_px(self, ref_px_1080: int) -> int:
        """Convert a 1080p-reference pixel value to actual proc-resolution pixels."""
        if ref_px_1080 <= 0:
            return 0
        scale = self._video_h / 1080.0
        actual = ref_px_1080 * scale
        return max(1, int(round(actual / max(1, self.params.proc_div))))

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        log.info("worker starting on %s", self.video_path)
        try:
            self._cap = cv2.VideoCapture(self.video_path)
            if not self._cap.isOpened():
                log.error("could not open video: %s", self.video_path)
                self.finished.emit()
                return

            fps = self._cap.get(cv2.CAP_PROP_FPS) or 24.0
            w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            n_frames = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))
            self._video_h = h if h > 0 else 1080
            log.info("video: %dx%d @ %.2ffps  %d frames  scale=%.2fx", w, h, fps, n_frames, self._video_h / 1080.0)

            frame_period = 1.0 / fps
            self._frame_period = frame_period
            next_t = time.monotonic()

            # rolling perf stats
            stats_window = 60
            proc_times: list[float] = []
            stats_t = time.monotonic()
            n_dropped = 0

            while self._running:
                if self.params.paused:
                    time.sleep(0.02)
                    next_t = time.monotonic()
                    continue

                ok, frame_bgr = self._cap.read()
                if not ok:
                    log.debug("EOF, looping")
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    self._prev_gray_small = None
                    self._activity = None
                    continue

                t0 = time.monotonic()
                qimage = self._process(frame_bgr)
                proc_times.append(time.monotonic() - t0)
                self.frame_ready.emit(qimage)

                next_t += frame_period
                sleep_for = next_t - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    n_dropped += 1
                    next_t = time.monotonic()

                # log perf every ~stats_window frames
                if len(proc_times) >= stats_window:
                    avg = sum(proc_times) / len(proc_times) * 1000
                    p95 = sorted(proc_times)[int(len(proc_times) * 0.95)] * 1000
                    real_fps = stats_window / max(1e-6, time.monotonic() - stats_t)
                    log.info(
                        "perf: proc=%.1fms avg / %.1fms p95   real_fps=%.1f   late=%d/%d   div=%d feather=%dpx persist=%.1fs edge=%.2f minR=%d",
                        avg, p95, real_fps, n_dropped, stats_window,
                        self.params.proc_div, self.params.feather_px_1080,
                        self.params.persist_seconds, self.params.edge_sensitivity,
                        self.params.min_region_px_1080,
                    )
                    proc_times.clear()
                    stats_t = time.monotonic()
                    n_dropped = 0
        except Exception:
            log.exception("worker crashed")
        finally:
            if self._cap is not None:
                self._cap.release()
            log.info("worker finished")
            self.finished.emit()

    def _process(self, frame_bgr: np.ndarray) -> QImage:
        h, w = frame_bgr.shape[:2]
        div = max(1, int(self.params.proc_div))
        pw, ph = max(1, w // div), max(1, h // div)

        # downscaled gray for diff
        small = cv2.resize(frame_bgr, (pw, ph), interpolation=cv2.INTER_AREA)
        small_gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if self._prev_gray_small is None or self._prev_gray_small.shape != small_gray.shape:
            small_mask = np.zeros((ph, pw), dtype=np.uint8)
        else:
            diff = cv2.absdiff(small_gray, self._prev_gray_small)
            t = max(1, int(self.params.threshold * 255))
            lo = max(1, t // 2)
            hi = max(lo + 1, (t * 3) // 2)
            scale = 255 // (hi - lo) if (hi - lo) > 0 else 255
            shifted = cv2.subtract(diff, np.array([lo], dtype=np.uint8))
            small_mask = cv2.convertScaleAbs(shifted, alpha=scale, beta=0)
        self._prev_gray_small = small_gray

        # ── edge sensitivity: weight motion by local edge strength ──
        # rationale: real motion is mostly carried by textured/edged content (faces, objects).
        # smooth backgrounds that "flicker" from compression noise have little edge content.
        es = max(0.0, min(1.0, float(self.params.edge_sensitivity)))
        if es > 0.0 and small_mask.any():
            # int16 sobels → uint8 magnitudes via convertScaleAbs (handles abs+saturation safely)
            sx = cv2.Sobel(small_gray, cv2.CV_16S, 1, 0, ksize=3)
            sy = cv2.Sobel(small_gray, cv2.CV_16S, 0, 1, ksize=3)
            edge = cv2.add(cv2.convertScaleAbs(sx), cv2.convertScaleAbs(sy))
            # build gate uint8 image: (1-es)*255 (no edge) .. 255 (max edge), then multiply
            floor_u8 = int(round((1.0 - es) * 255))
            edge_part = cv2.convertScaleAbs(edge, alpha=es)  # range 0 .. es*255
            gate = cv2.add(np.full(edge.shape, floor_u8, dtype=np.uint8), edge_part)
            small_mask = cv2.multiply(small_mask, gate, scale=1.0 / 255.0)

        # ── scene-cut detection ──
        if self.params.scene_cut_ignore and small_mask.size > 0:
            active_frac = float(cv2.countNonZero(small_mask)) / small_mask.size
            if active_frac > self.params.scene_cut_thresh:
                small_mask = np.zeros_like(small_mask)

        # ── min region size: drop small noise blobs ──
        min_ext_proc = self._ref_to_proc_px(self.params.min_region_px_1080)
        if min_ext_proc > 0:
            min_area = min_ext_proc * min_ext_proc
            _, bin_mask = cv2.threshold(small_mask, 32, 255, cv2.THRESH_BINARY)
            n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(bin_mask, connectivity=8)
            keep = np.zeros(n_labels, dtype=np.uint8)
            for i in range(1, n_labels):
                if stats[i, cv2.CC_STAT_AREA] >= min_area:
                    keep[i] = 1
            keep_map = keep[labels].astype(np.uint8) * 255
            small_mask = cv2.bitwise_and(small_mask, keep_map)

        # ── persistence ──
        persist = max(0.0, min(5.0, float(self.params.persist_seconds)))
        if persist > 0.0:
            if self._activity is None or self._activity.shape != small_mask.shape:
                self._activity = small_mask.copy()
            else:
                decay_u8 = max(1, int(255 * (self._frame_period / persist)))
                decayed = cv2.subtract(self._activity, np.array([decay_u8], dtype=np.uint8))
                self._activity = cv2.max(decayed, small_mask)
            small_mask = self._activity
        else:
            self._activity = None

        # ── padding: dilate active region outward (in 1080p-ref pixels) ──
        pad_proc = self._ref_to_proc_px(self.params.padding_px_1080)
        if pad_proc > 0:
            k = 2 * pad_proc + 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
            small_mask = cv2.dilate(small_mask, kernel)

        # ── feather: gaussian blur for soft edges (in 1080p-ref pixels) ──
        feather_proc = self._ref_to_proc_px(self.params.feather_px_1080)
        r = max(1, feather_proc) | 1
        small_mask = cv2.GaussianBlur(small_mask, (r, r), 0)

        # ── feather falloff: power curve on the mask shape ──
        # < 1 softer fade (large halo) | = 1 linear | > 1 sharper edge
        falloff = max(0.1, min(5.0, float(self.params.feather_falloff)))
        if abs(falloff - 1.0) > 0.01:
            # cache the LUT and rebuild only when the falloff value changes
            if self._falloff_lut is None or self._falloff_lut[0] != falloff:
                xs = np.arange(256, dtype=np.float32) / 255.0
                lut = np.clip(np.power(xs, falloff) * 255.0, 0, 255).astype(np.uint8)
                self._falloff_lut = (falloff, lut)
            small_mask = cv2.LUT(small_mask, self._falloff_lut[1])

        # upscale mask to full res with linear interpolation (smooth feathering)
        mask_u8 = cv2.resize(small_mask, (w, h), interpolation=cv2.INTER_LINEAR)

        mode = self.params.debug_mode
        if mode == 3:
            # effect off: full opaque
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            alpha = np.full((h, w), 255, dtype=np.uint8)
            rgba = cv2.merge([rgb[..., 0], rgb[..., 1], rgb[..., 2], alpha])
        elif mode == 1:
            # heatmap: red where active, blue where static — opaque
            inv = cv2.bitwise_not(mask_u8)
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            green = cv2.convertScaleAbs(mask_u8, alpha=30 / 255.0, beta=0)
            heat = cv2.merge([mask_u8, green, inv])  # R, G, B
            blended = cv2.addWeighted(rgb, 0.3, heat, 0.7, 0)
            alpha = np.full((h, w), 255, dtype=np.uint8)
            rgba = cv2.merge([blended[..., 0], blended[..., 1], blended[..., 2], alpha])
        elif mode == 2:
            # binary mask: white=active, black=static
            _, bw = cv2.threshold(mask_u8, 127, 255, cv2.THRESH_BINARY)
            alpha = np.full((h, w), 255, dtype=np.uint8)
            rgba = cv2.merge([bw, bw, bw, alpha])
        else:
            # normal: per-pixel alpha → real transparency
            floor_u8 = max(0, min(255, int(self.params.alpha_floor * 255)))
            # alpha = floor + (255 - floor) * mask/255  → all uint8 ops
            scale_u8 = 255 - floor_u8
            scaled = cv2.convertScaleAbs(mask_u8, alpha=scale_u8 / 255.0, beta=floor_u8)
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            # premultiply: R'=R*a/255 — use cv2 multiply (vectorized C path)
            a3 = cv2.merge([scaled, scaled, scaled])
            premul = cv2.multiply(rgb, a3, scale=1.0 / 255.0)
            rgba = cv2.merge([premul[..., 0], premul[..., 1], premul[..., 2], scaled])

        # build QImage referencing numpy buffer, then deep-copy so it owns its data.
        # without copy(): we'd reuse the buffer next frame and blow up the previous
        # QImage that's still in flight through the signal/slot queue → silent crash.
        rgba = np.ascontiguousarray(rgba)
        qi = QImage(
            rgba.data, w, h, w * 4,
            QImage.Format.Format_RGBA8888_Premultiplied,
        ).copy()
        return qi


# ─── transparent video window ───────────────────────────────────────────────
class PlayerWindow(QWidget):
    def __init__(self, video_path: str, params: Params) -> None:
        super().__init__()
        self.params = params
        self.setWindowTitle("travis-player")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAcceptDrops(True)
        self._video_path = video_path

        # restore saved geometry (or use defaults)
        self.resize(max(200, params.player_w), max(150, params.player_h))
        if params.player_x >= 0 and params.player_y >= 0:
            self.move(params.player_x, params.player_y)

        self._pixmap: QPixmap | None = None
        self._drag_pos: QPoint | None = None
        self._osd_text: str = ""
        self._osd_until: float = 0.0
        self._hovered: bool = False
        self.setMouseTracking(True)  # get mouseMove without button held
        self._chrome_h = 28          # height of the top drag bar
        self._grip_size = 22         # bottom-right resize grip

        self._thread = QThread(self)
        self._worker = FrameWorker(video_path, self.params)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

        # audio sidecar (IPC-driven, can pause/resume without restart)
        self._audio = AudioController(video_path)
        self._audio.start()

    def _on_frame(self, qimage: QImage) -> None:
        self._pixmap = QPixmap.fromImage(qimage)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)

        if self._pixmap is not None:
            scaled = self._pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.drawPixmap(x, y, scaled)

        # ── chrome: visible drag bar + resize grip when hovered ──
        if self._hovered and not self.isFullScreen():
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setPen(Qt.PenStyle.NoPen)

            # top drag bar
            p.setBrush(QBrush(QColor(20, 20, 20, 170)))
            p.drawRect(0, 0, self.width(), self._chrome_h)
            p.setPen(QColor(255, 255, 255, 200))
            p.drawText(10, self._chrome_h - 8, "⋮⋮  drag to move    [F11] fullscreen    [Q] quit")

            # bottom-right resize grip — a triangle
            g = self._grip_size
            poly = QPolygon([
                QPoint(self.width(),     self.height() - g),
                QPoint(self.width(),     self.height()),
                QPoint(self.width() - g, self.height()),
            ])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(255, 255, 255, 180)))
            p.drawPolygon(poly)
            # grip lines for clarity
            p.setPen(QColor(20, 20, 20, 230))
            for i in range(4):
                off = 4 + i * 4
                p.drawLine(self.width() - off, self.height() - 1,
                           self.width() - 1,    self.height() - off)

        if time.monotonic() < self._osd_until and self._osd_text:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            y_top = self._chrome_h if self._hovered else 0
            p.fillRect(0, y_top, self.width(), 30, Qt.GlobalColor.black)
            p.setPen(Qt.GlobalColor.white)
            p.drawText(10, y_top + 22, self._osd_text)

    def osd(self, text: str, seconds: float = 1.5) -> None:
        self._osd_text = text
        self._osd_until = time.monotonic() + seconds
        self.update()

    # drag-to-move + edge-resize for the frameless window
    _RESIZE_MARGIN = 12  # pixels from edge counted as resize zone

    def _edge_at(self, pos: QPoint) -> Qt.Edge | None:
        m = self._RESIZE_MARGIN
        g = self._grip_size
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        # the visible bottom-right grip zone always counts as resize
        if x > w - g and y > h - g:
            return Qt.Edge.RightEdge | Qt.Edge.BottomEdge
        left, right = x < m, x > w - m
        top, bottom = y < m, y > h - m
        edges_val = 0
        if left:   edges_val |= int(Qt.Edge.LeftEdge.value)
        if right:  edges_val |= int(Qt.Edge.RightEdge.value)
        if top:    edges_val |= int(Qt.Edge.TopEdge.value)
        if bottom: edges_val |= int(Qt.Edge.BottomEdge.value)
        return Qt.Edge(edges_val) if edges_val else None

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton and not self.isFullScreen():
            edge = self._edge_at(e.position().toPoint())
            wh = self.windowHandle()
            if edge is not None and wh is not None:
                wh.startSystemResize(edge)
                return
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        # update cursor when hovering edges
        if not self.isFullScreen() and not (e.buttons() & Qt.MouseButton.LeftButton):
            edge = self._edge_at(e.position().toPoint())
            cursor_map = {
                Qt.Edge.LeftEdge:  Qt.CursorShape.SizeHorCursor,
                Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
                Qt.Edge.TopEdge:   Qt.CursorShape.SizeVerCursor,
                Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
                Qt.Edge.LeftEdge | Qt.Edge.TopEdge:    Qt.CursorShape.SizeFDiagCursor,
                Qt.Edge.RightEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeFDiagCursor,
                Qt.Edge.RightEdge | Qt.Edge.TopEdge:    Qt.CursorShape.SizeBDiagCursor,
                Qt.Edge.LeftEdge | Qt.Edge.BottomEdge:  Qt.CursorShape.SizeBDiagCursor,
            }
            self.setCursor(cursor_map.get(edge, Qt.CursorShape.ArrowCursor))

        if self._drag_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, _e: QMouseEvent) -> None:
        self._drag_pos = None

    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.toggle_fullscreen()

    def enterEvent(self, _e) -> None:
        self._hovered = True
        self.update()

    def leaveEvent(self, _e) -> None:
        self._hovered = False
        self.unsetCursor()
        self.update()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        k = e.key()
        if k in (Qt.Key.Key_Q, Qt.Key.Key_Escape):
            QApplication.instance().quit()
        elif k == Qt.Key.Key_Space:
            self.set_paused(not self.params.paused)
        elif k == Qt.Key.Key_F11 or (k == Qt.Key.Key_F and e.modifiers() == Qt.KeyboardModifier.NoModifier):
            self.toggle_fullscreen()
        elif k == Qt.Key.Key_R:
            self.reset_geometry()

    def reset_geometry(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        self.resize(960, 540)
        # center on the screen we're currently on
        screen = self.screen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(
                geo.x() + (geo.width() - self.width()) // 2,
                geo.y() + (geo.height() - self.height()) // 2,
            )
        self.osd("geometry reset")
        self._save_geometry()

    def _save_geometry(self) -> None:
        if self.isFullScreen():
            return  # don't overwrite saved geo with fullscreen dimensions
        self.params.player_x = self.x()
        self.params.player_y = self.y()
        self.params.player_w = self.width()
        self.params.player_h = self.height()
        self.params.save()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._save_geometry()

    def moveEvent(self, e) -> None:
        super().moveEvent(e)
        self._save_geometry()

    def set_paused(self, paused: bool) -> None:
        self.params.paused = paused
        self._audio.set_paused(paused)
        self.osd("paused" if paused else "playing")

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.osd("windowed")
        else:
            self.showFullScreen()
            self.osd("fullscreen — F11 to exit")

    # ── drag-and-drop for files and stream URIs ──
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        md = e.mimeData()
        if md.hasUrls() or md.hasText():
            e.acceptProposedAction()
            self.osd("drop to load")

    def dropEvent(self, e: QDropEvent) -> None:
        md = e.mimeData()
        source: str | None = None
        if md.hasUrls():
            url = md.urls()[0]
            # local file → use path; remote → keep as full URL string
            source = url.toLocalFile() if url.isLocalFile() else url.toString()
        elif md.hasText():
            source = md.text().strip()
        if source:
            log.info("drop received: %s", source)
            self._load_source(source)
            self.osd(f"loading: {source[:60]}", 2.5)

    def _load_source(self, source: str) -> None:
        """Stop current worker+audio and start fresh with the new source."""
        log.info("swapping source -> %s", source)

        # stop current worker thread
        self._worker.stop()
        self._thread.quit()
        self._thread.wait(2000)

        # always stop the previous audio first so it can't leak
        try:
            self._audio.stop()
        except Exception:
            log.exception("failed stopping old audio sidecar")

        self._video_path = source

        self._thread = QThread(self)
        self._worker = FrameWorker(source, self.params)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

        # new audio with a *fresh* pipe name so nothing collides with the previous
        self._audio = AudioController(source, pipe_name=rf"\\.\pipe\travis-audio-{os.getpid()}-{int(time.monotonic()*1000)}")
        self._audio.start()
        if self.params.paused:
            self._audio.set_paused(True)

    def cleanup(self) -> None:
        log.info("cleanup starting")
        try:
            self.params.save()
        except Exception:
            log.exception("settings save failed")
        self._worker.stop()
        self._thread.quit()
        self._thread.wait(2000)
        try:
            self._audio.stop()
        except Exception:
            log.exception("audio stop failed")
        log.info("cleanup done")

    def closeEvent(self, _e) -> None:
        self.cleanup()


# ─── control panel window ───────────────────────────────────────────────────
def _slider(lo: int, hi: int, value: int, on_change) -> QSlider:
    s = QSlider(Qt.Orientation.Horizontal)
    s.setRange(lo, hi)
    s.setValue(value)
    s.valueChanged.connect(on_change)
    return s


class PresetCombo(QComboBox):
    """Dropdown that re-scans the presets dir each time it's opened.
    Lets the user delete preset files outside the app and have it reflect."""

    def showPopup(self) -> None:
        current = self.currentText()
        self.blockSignals(True)
        self.clear()
        self.addItems(list_presets())
        # try to restore the current selection if it's still present
        idx = self.findText(current)
        if idx >= 0:
            self.setCurrentIndex(idx)
        self.blockSignals(False)
        super().showPopup()


class ControlPanel(QWidget):
    def __init__(self, params: Params, player: PlayerWindow) -> None:
        super().__init__()
        self.params = params
        self.player = player
        self.setWindowTitle("travis controls")
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)
        self.setAcceptDrops(True)

        # restore saved geometry
        self.resize(max(300, params.controls_w), max(400, params.controls_h))
        if params.controls_x >= 0 and params.controls_y >= 0:
            self.move(params.controls_x, params.controls_y)

        # scrollable so the panel works on small displays
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        # ── presets group ──
        gb_preset = QGroupBox("Presets")
        gp = QVBoxLayout(gb_preset)
        gp.addWidget(QLabel(
            f"Saved as JSON in {PRESETS_DIR.name}/. Delete files there to remove."
        ))
        load_row = QHBoxLayout()
        self.preset_dd = PresetCombo()
        self.preset_dd.addItems(list_presets())
        self.preset_dd.setToolTip("Click to refresh from disk (re-scans the presets/ folder each time).")
        load_row.addWidget(self.preset_dd, stretch=2)
        btn_load = QPushButton("Load")
        btn_load.clicked.connect(self._on_load_preset)
        load_row.addWidget(btn_load)
        gp.addLayout(load_row)
        save_row = QHBoxLayout()
        self.preset_name = QLineEdit()
        self.preset_name.setPlaceholderText("name to save current settings as…")
        self.preset_name.returnPressed.connect(self._on_save_preset)
        save_row.addWidget(self.preset_name, stretch=2)
        btn_save_p = QPushButton("Save")
        btn_save_p.clicked.connect(self._on_save_preset)
        save_row.addWidget(btn_save_p)
        gp.addLayout(save_row)
        v.addWidget(gb_preset)

        # ── view group ──
        gb_view = QGroupBox("View")
        gv = QVBoxLayout(gb_view)
        gv.addWidget(QLabel("Debug mode"))
        self.cb_mode = QComboBox()
        self.cb_mode.addItems([
            "Normal (transparent compositing)",
            "Heatmap (red = active, blue = static)",
            "Binary mask (white = active)",
            "Effect off (full opaque video)",
        ])
        self.cb_mode.setCurrentIndex(params.debug_mode)
        self.cb_mode.currentIndexChanged.connect(self._on_mode)
        self.cb_mode.setToolTip("How the masked video is displayed. Use Heatmap to tune motion settings visually.")
        gv.addWidget(self.cb_mode)
        v.addWidget(gb_view)

        # ── visibility group ──
        gb_vis = QGroupBox("Visibility")
        gvis = QVBoxLayout(gb_vis)
        self.lbl_alpha = QLabel()
        self.lbl_alpha.setWordWrap(True)
        gvis.addWidget(self.lbl_alpha)
        self.sl_alpha = _slider(0, 100, int(params.alpha_floor * 100), self._on_alpha)
        self.sl_alpha.setToolTip(
            "How visible static regions are. 0 = fully transparent (see-through to desktop). "
            "1.0 = fully opaque (no transparency effect)."
        )
        gvis.addWidget(self.sl_alpha)
        v.addWidget(gb_vis)

        # ── motion detection group ──
        gb_mot = QGroupBox("Motion detection")
        gm = QVBoxLayout(gb_mot)

        self.lbl_thresh = QLabel()
        self.lbl_thresh.setWordWrap(True)
        gm.addWidget(self.lbl_thresh)
        self.sl_thresh = _slider(1, 100, int(params.threshold * 1000), self._on_thresh)
        self.sl_thresh.setToolTip(
            "How much a pixel's brightness must change between frames to count as motion. "
            "Lower = more sensitive (catches subtle motion + noise). Higher = only big changes register."
        )
        gm.addWidget(self.sl_thresh)

        self.lbl_edge = QLabel()
        self.lbl_edge.setWordWrap(True)
        gm.addWidget(self.lbl_edge)
        self.sl_edge = _slider(0, 100, int(params.edge_sensitivity * 100), self._on_edge)
        self.sl_edge.setToolTip(
            "Weight motion by local edge density. At 0, a flickering smooth background counts the same as a moving face. "
            "At 1, only motion in textured / edged regions is kept — kills compression noise on flat backgrounds."
        )
        gm.addWidget(self.sl_edge)

        self.lbl_minr = QLabel()
        self.lbl_minr.setWordWrap(True)
        gm.addWidget(self.lbl_minr)
        self.sl_minr = _slider(0, 80, params.min_region_px_1080, self._on_minr)
        self.sl_minr.setToolTip(
            "Reject motion blobs smaller than this many pixels (1080p reference). "
            "Removes single-pixel film grain noise without affecting real moving objects. 0 = off."
        )
        gm.addWidget(self.sl_minr)
        v.addWidget(gb_mot)

        # ── region shape group ──
        gb_shape = QGroupBox("Region shape")
        gs = QVBoxLayout(gb_shape)

        self.lbl_padding = QLabel()
        self.lbl_padding.setWordWrap(True)
        gs.addWidget(self.lbl_padding)
        self.sl_padding = _slider(0, 60, params.padding_px_1080, self._on_padding)
        self.sl_padding.setToolTip(
            "Expand each active region outward by this many pixels (1080p reference) before fading. "
            "Useful for keeping fast-moving edges fully visible instead of partially transparent."
        )
        gs.addWidget(self.sl_padding)

        self.lbl_feather = QLabel()
        self.lbl_feather.setWordWrap(True)
        gs.addWidget(self.lbl_feather)
        self.sl_feather = _slider(1, 80, params.feather_px_1080, self._on_feather)
        self.sl_feather.setToolTip(
            "Width of the soft transition between active and static regions, in pixels (1080p reference). "
            "Larger = gentler fade, smaller = harder edge."
        )
        gs.addWidget(self.sl_feather)

        self.lbl_falloff = QLabel()
        self.lbl_falloff.setWordWrap(True)
        gs.addWidget(self.lbl_falloff)
        self.sl_falloff = _slider(20, 300, int(params.feather_falloff * 100), self._on_falloff)
        self.sl_falloff.setToolTip(
            "Shape of the feather curve. <1.0 = soft falloff (large translucent halo). "
            "1.0 = linear. >1.0 = harder cutoff (most of the region stays opaque, edge drops fast)."
        )
        gs.addWidget(self.sl_falloff)
        v.addWidget(gb_shape)

        # ── temporal group ──
        gb_t = QGroupBox("Temporal")
        gt = QVBoxLayout(gb_t)
        self.lbl_persist = QLabel()
        self.lbl_persist.setWordWrap(True)
        gt.addWidget(self.lbl_persist)
        self.sl_persist = _slider(0, 50, int(params.persist_seconds * 10), self._on_persist)
        self.sl_persist.setToolTip(
            "Recently-active regions stay visible for this long after motion stops. "
            "0 = no persistence (immediately fades). Higher = motion trails."
        )
        gt.addWidget(self.sl_persist)

        self.cb_scene = QCheckBox("Ignore scene cuts (drop frames where most pixels changed at once)")
        self.cb_scene.setChecked(params.scene_cut_ignore)
        self.cb_scene.toggled.connect(self._on_scene_toggle)
        self.cb_scene.setToolTip(
            "Hard cuts and global brightness flashes change every pixel and look like motion everywhere. "
            "When this is on, frames where the active fraction exceeds the threshold below are discarded."
        )
        gt.addWidget(self.cb_scene)
        self.lbl_scene = QLabel()
        self.lbl_scene.setWordWrap(True)
        gt.addWidget(self.lbl_scene)
        self.sl_scene = _slider(20, 99, int(params.scene_cut_thresh * 100), self._on_scene_thresh)
        self.sl_scene.setToolTip(
            "Fraction of the frame that must be 'active' for the frame to be considered a scene cut. "
            "0.7 = a frame with >70% of pixels marked as motion is a cut."
        )
        gt.addWidget(self.sl_scene)
        v.addWidget(gb_t)

        # ── compute group ──
        gb_c = QGroupBox("Compute")
        gc = QVBoxLayout(gb_c)
        self.lbl_proc = QLabel()
        self.lbl_proc.setWordWrap(True)
        gc.addWidget(self.lbl_proc)
        self.sl_proc = _slider(1, 16, params.proc_div, self._on_proc)
        self.sl_proc.setToolTip(
            "Diff math runs at video resolution / this divisor. 8 means each axis is 1/8 (so 1/64 the pixel work). "
            "Lower = more detail in the mask but more CPU. Higher = faster but coarser blocks."
        )
        gc.addWidget(self.sl_proc)
        v.addWidget(gb_c)

        # ── action buttons ──
        row = QHBoxLayout()
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self._toggle_pause)
        row.addWidget(self.btn_pause)
        self.btn_full = QPushButton("Fullscreen")
        self.btn_full.clicked.connect(player.toggle_fullscreen)
        row.addWidget(self.btn_full)
        btn_quit = QPushButton("Quit")
        btn_quit.clicked.connect(lambda: QApplication.instance().quit())
        row.addWidget(btn_quit)
        v.addLayout(row)

        v.addStretch()
        self._refresh_labels()

    def _refresh_labels(self) -> None:
        p = self.params
        self.lbl_alpha.setText(
            f"Static-region opacity:  {p.alpha_floor:.2f}    "
            f"({'fully transparent' if p.alpha_floor == 0 else 'fully opaque' if p.alpha_floor == 1 else f'{int(p.alpha_floor * 100)}% visible'})"
        )
        self.lbl_thresh.setText(f"Motion threshold:  {p.threshold:.3f}    (per-pixel brightness change, 0–1)")
        self.lbl_edge.setText(
            f"Edge sensitivity:  {p.edge_sensitivity:.2f}    "
            f"({'off' if p.edge_sensitivity == 0 else 'only textured regions' if p.edge_sensitivity >= 0.95 else 'partial weighting'})"
        )
        self.lbl_minr.setText(
            f"Min region size:  {p.min_region_px_1080} pixels (@1080p)    "
            f"({'off' if p.min_region_px_1080 == 0 else f'reject blobs smaller than {p.min_region_px_1080}×{p.min_region_px_1080}'})"
        )
        self.lbl_padding.setText(f"Padding (expand region):  {p.padding_px_1080} pixels (@1080p)")
        self.lbl_feather.setText(f"Feather (edge softness):  {p.feather_px_1080} pixels (@1080p)")
        self.lbl_falloff.setText(
            f"Feather falloff curve:  {p.feather_falloff:.2f}    "
            f"({'soft halo' if p.feather_falloff < 0.9 else 'linear' if p.feather_falloff < 1.1 else 'sharp cutoff'})"
        )
        persist_label = f"{p.persist_seconds:.1f}s" if p.persist_seconds > 0 else "off"
        self.lbl_persist.setText(f"Persistence (motion trails):  {persist_label}")
        self.lbl_scene.setText(f"Scene-cut active-fraction threshold:  {p.scene_cut_thresh:.2f}")
        self.lbl_proc.setText(
            f"Compute divisor:  1/{p.proc_div}    "
            f"(diff math at 1/{p.proc_div} of video resolution per axis = 1/{p.proc_div * p.proc_div} the pixels)"
        )

    def _save(self) -> None:
        self.params.save()

    # ── presets ──
    def _on_save_preset(self) -> None:
        name = self.preset_name.text().strip()
        if not name:
            self.player.osd("preset name required")
            return
        path = self.params.save_preset(name)
        if path is None:
            self.player.osd("preset save failed")
            return
        self.preset_name.clear()
        # repopulate dropdown immediately and select the just-saved entry
        self.preset_dd.blockSignals(True)
        self.preset_dd.clear()
        self.preset_dd.addItems(list_presets())
        idx = self.preset_dd.findText(_safe_preset_name(name))
        if idx >= 0:
            self.preset_dd.setCurrentIndex(idx)
        self.preset_dd.blockSignals(False)
        self.player.osd(f"saved preset: {path.stem}")

    def _on_load_preset(self) -> None:
        name = self.preset_dd.currentText().strip()
        if not name:
            self.player.osd("no preset selected")
            return
        if not self.params.load_preset(name):
            self.player.osd(f"preset not found: {name}")
            return
        self._sync_widgets_from_params()
        self.params.save()
        self.player.osd(f"loaded preset: {name}")

    def _sync_widgets_from_params(self) -> None:
        """Push current self.params values back into every widget without
        re-firing the on-change handlers (which would write settings.json
        once per slider). Single _save() at the call site instead."""
        p = self.params
        widgets_and_values = [
            (self.cb_mode,    "setCurrentIndex", p.debug_mode),
            (self.sl_alpha,   "setValue",        int(p.alpha_floor * 100)),
            (self.sl_thresh,  "setValue",        int(p.threshold * 1000)),
            (self.sl_edge,    "setValue",        int(p.edge_sensitivity * 100)),
            (self.sl_minr,    "setValue",        p.min_region_px_1080),
            (self.sl_padding, "setValue",        p.padding_px_1080),
            (self.sl_feather, "setValue",        p.feather_px_1080),
            (self.sl_falloff, "setValue",        int(p.feather_falloff * 100)),
            (self.sl_persist, "setValue",        int(p.persist_seconds * 10)),
            (self.cb_scene,   "setChecked",      p.scene_cut_ignore),
            (self.sl_scene,   "setValue",        int(p.scene_cut_thresh * 100)),
            (self.sl_proc,    "setValue",        p.proc_div),
        ]
        for widget, setter, value in widgets_and_values:
            widget.blockSignals(True)
            getattr(widget, setter)(value)
            widget.blockSignals(False)
        self._refresh_labels()

    def _on_mode(self, idx: int) -> None:
        self.params.debug_mode = idx
        self._save()

    def _on_alpha(self, v: int) -> None:
        self.params.alpha_floor = v / 100.0
        self._refresh_labels(); self._save()

    def _on_thresh(self, v: int) -> None:
        self.params.threshold = v / 1000.0
        self._refresh_labels(); self._save()

    def _on_padding(self, v: int) -> None:
        self.params.padding_px_1080 = v
        self._refresh_labels(); self._save()

    def _on_feather(self, v: int) -> None:
        self.params.feather_px_1080 = max(1, v)
        self._refresh_labels(); self._save()

    def _on_falloff(self, v: int) -> None:
        self.params.feather_falloff = v / 100.0
        self._refresh_labels(); self._save()

    def _on_persist(self, v: int) -> None:
        self.params.persist_seconds = v / 10.0
        self._refresh_labels(); self._save()

    def _on_edge(self, v: int) -> None:
        self.params.edge_sensitivity = v / 100.0
        self._refresh_labels(); self._save()

    def _on_minr(self, v: int) -> None:
        self.params.min_region_px_1080 = v
        self._refresh_labels(); self._save()

    def _on_scene_toggle(self, on: bool) -> None:
        self.params.scene_cut_ignore = on
        self._save()

    def _on_scene_thresh(self, v: int) -> None:
        self.params.scene_cut_thresh = v / 100.0
        self._refresh_labels(); self._save()

    def _on_proc(self, v: int) -> None:
        self.params.proc_div = v
        self._refresh_labels(); self._save()

    def _toggle_pause(self) -> None:
        new_state = not self.params.paused
        self.player.set_paused(new_state)
        self.btn_pause.setText("Resume" if new_state else "Pause")

    def reset_geometry(self) -> None:
        self.resize(440, 720)
        screen = self.screen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.x() + 40, geo.y() + 40)

    def _save_geometry(self) -> None:
        self.params.controls_x = self.x()
        self.params.controls_y = self.y()
        self.params.controls_w = self.width()
        self.params.controls_h = self.height()
        self.params.save()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._save_geometry()

    def moveEvent(self, e) -> None:
        super().moveEvent(e)
        self._save_geometry()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() == Qt.Key.Key_R:
            self.reset_geometry()
            self.player.reset_geometry()
            self.player.osd("both windows reset")
        else:
            super().keyPressEvent(e)

    # forward drops to the player
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        md = e.mimeData()
        if md.hasUrls() or md.hasText():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        md = e.mimeData()
        source: str | None = None
        if md.hasUrls():
            url = md.urls()[0]
            source = url.toLocalFile() if url.isLocalFile() else url.toString()
        elif md.hasText():
            source = md.text().strip()
        if source:
            log.info("drop on controls: %s", source)
            self.player._load_source(source)
            self.player.osd(f"loading: {source[:60]}", 2.5)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: travis_player.py <video.mp4>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    if not Path(path).exists():
        print(f"not found: {path}", file=sys.stderr)
        return 2

    _setup_logging()
    log.info("=" * 60)
    log.info("travis-player starting  argv=%r  cwd=%s", sys.argv, Path.cwd())

    global _job_handle
    _job_handle = _create_job_for_children()
    log.info("job object: %s", "created" if _job_handle else "FAILED — children may orphan")

    app = QApplication(sys.argv)
    params = Params()
    params.load()
    log.info("params: %s", asdict(params))

    player = PlayerWindow(path, params)
    controls = ControlPanel(params, player)

    # cleanup on quit (covers Esc, Q, controls Quit, window close)
    def _cleanup() -> None:
        try:
            player.cleanup()
        except Exception:
            pass
    app.aboutToQuit.connect(_cleanup)
    atexit.register(_cleanup)

    player.show()
    controls.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

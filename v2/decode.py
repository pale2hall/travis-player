"""PyAV decoder with motion-vector export.

yields, per frame, the decoded RGB plus the codec's own motion vectors (the data v1 threw
away). the MV-export flag (`flags2 +export_mvs`) must be set on the codec context *before*
the decode loop — it is read at decode time. proven in v2/spikes/dump_mvs.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import av
import numpy as np

log = logging.getLogger("travis.v2")

# ffmpeg AVMotionVector (libavutil/motion_vector.h). align=True reproduces the C natural
# alignment: a 2-byte pad before uint64 `flags`, a 6-byte tail pad → 40 bytes total.
MV_DTYPE = np.dtype([
    ("source", "<i4"),
    ("w", "u1"), ("h", "u1"),
    ("src_x", "<i2"), ("src_y", "<i2"),
    ("dst_x", "<i2"), ("dst_y", "<i2"),
    ("flags", "<u8"),
    ("motion_x", "<i4"), ("motion_y", "<i4"),
    ("motion_scale", "<u2"),
], align=True)

_PICT = {0: "NONE", 1: "I", 2: "P", 3: "B"}


@dataclass
class DecodedFrame:
    rgb: np.ndarray          # (H, W, 3) uint8, RGB
    mvs: np.ndarray | None   # structured MV array (MV_DTYPE) or None
    pict_type: str           # 'I' | 'P' | 'B' | 'NONE'
    pts: float               # presentation time in seconds (0.0 if unknown)
    width: int
    height: int


class Decoder:
    """Pull-style decoder. `read()` returns the next DecodedFrame or None at EOF.
    Seeking and looping are driven externally by the worker."""

    def __init__(self, source: str) -> None:
        self.source = source
        self._container: av.container.InputContainer | None = None
        self._stream = None
        self._frames = None
        self.fps: float = 24.0
        self.duration: float = 0.0   # seconds
        self.width: int = 0
        self.height: int = 0
        self.codec: str = ""

    def open(self) -> None:
        self._container = av.open(self.source)
        self._stream = self._container.streams.video[0]
        cc = self._stream.codec_context
        # enable motion-vector export before decoding
        try:
            cc.options = {"flags2": "+export_mvs"}
        except Exception:
            log.exception("could not set export_mvs flag — MVs may be empty")
        self._stream.thread_type = "AUTO"
        self.width = cc.width
        self.height = cc.height
        self.codec = cc.name or ""
        self.fps = float(self._stream.average_rate or self._stream.guessed_rate or 24.0)
        if self._stream.duration is not None and self._stream.time_base is not None:
            self.duration = float(self._stream.duration * self._stream.time_base)
        elif self._container.duration is not None:
            self.duration = float(self._container.duration / av.time_base)
        self._frames = self._container.decode(self._stream)
        log.info("decoder open: %dx%d @ %.3ffps  dur=%.1fs  codec=%s",
                 self.width, self.height, self.fps, self.duration, cc.name)

    def _extract_mvs(self, frame: av.VideoFrame) -> np.ndarray | None:
        for sd in frame.side_data:
            if "MOTION" in str(sd.type).upper():
                try:
                    arr = np.frombuffer(bytes(sd), dtype=MV_DTYPE)
                    return arr if len(arr) else None
                except Exception:
                    return None
        return None

    def read(self) -> DecodedFrame | None:
        if self._frames is None:
            return None
        try:
            frame = next(self._frames)
        except StopIteration:
            return None
        rgb = frame.to_ndarray(format="rgb24")
        pts = float(frame.pts * self._stream.time_base) if frame.pts is not None else 0.0
        return DecodedFrame(
            rgb=rgb,
            mvs=self._extract_mvs(frame),
            pict_type=_PICT.get(int(frame.pict_type), "?"),
            pts=pts,
            width=rgb.shape[1],
            height=rgb.shape[0],
        )

    def seek(self, pos_frac: float) -> None:
        """Seek to a fraction (0..1) of the duration. Snaps to the prior keyframe."""
        if self._container is None or self._stream is None:
            return
        pos_frac = max(0.0, min(1.0, pos_frac))
        if self.duration <= 0:
            return
        target = pos_frac * self.duration
        ts = int(target / self._stream.time_base)
        try:
            self._container.seek(ts, stream=self._stream, backward=True, any_frame=False)
        except Exception:
            log.exception("seek failed")
        self._frames = self._container.decode(self._stream)

    def rewind(self) -> None:
        self.seek(0.0)

    def close(self) -> None:
        if self._container is not None:
            try:
                self._container.close()
            except Exception:
                pass
        self._container = None
        self._frames = None

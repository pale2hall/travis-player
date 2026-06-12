"""master playback clock — the A/V drift fix.

v1's video was paced by `time.sleep(frame_period)`, which accumulates error and drops frames
while the audio sidecar plays at true real time → the two slide apart over minutes. v2 makes
video chase a single wall-clock master (a proxy for the audio clock, since mpv plays at real
time): each decoded frame is presented against `clock.now()`. frames that fall behind are
dropped to catch up, frames that run ahead wait. that locks A/V by construction without
needing IPC readback from mpv.
"""

from __future__ import annotations

import time


class MasterClock:
    """Monotonic playback clock in seconds, pause-aware and seekable."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._base = 0.0          # playback position at _t0
        self._paused = False
        self._pause_pos = 0.0

    def reset(self, pos: float = 0.0) -> None:
        self._t0 = time.monotonic()
        self._base = pos
        if self._paused:
            self._pause_pos = pos

    def now(self) -> float:
        """Current playback position in seconds."""
        if self._paused:
            return self._pause_pos
        return self._base + (time.monotonic() - self._t0)

    def set_paused(self, paused: bool) -> None:
        if paused == self._paused:
            return
        if paused:
            self._pause_pos = self.now()
            self._paused = True
        else:
            self._paused = False
            self._t0 = time.monotonic()
            self._base = self._pause_pos

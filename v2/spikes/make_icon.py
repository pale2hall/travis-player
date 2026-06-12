"""generate assets/travis.ico — a motion-tile grid glyph matching the app's debug views.
uses cv2 to draw + encode PNG, then wraps it in a minimal (PNG-in-ICO) container so no
image deps beyond opencv are needed."""

from __future__ import annotations

import struct
from pathlib import Path

import cv2
import numpy as np

OUT = Path(__file__).resolve().parents[1].parent / "assets" / "travis.ico"
N = 256


def rounded_mask(h: int, w: int, r: int) -> np.ndarray:
    m = np.zeros((h, w), np.uint8)
    cv2.rectangle(m, (r, 0), (w - r, h), 255, -1)
    cv2.rectangle(m, (0, r), (w, h - r), 255, -1)
    for cx, cy in [(r, r), (w - r, r), (r, h - r), (w - r, h - r)]:
        cv2.circle(m, (cx, cy), r, 255, -1)
    return m


def main() -> None:
    # BGRA canvas
    img = np.zeros((N, N, 4), np.uint8)
    bg = rounded_mask(N, N, 52)
    img[bg > 0] = (26, 22, 18, 255)  # dark charcoal panel, opaque

    # 4x4 tile grid; a diagonal sweep is "lit" (the motion), in the debug-view green
    margin, gap = 40, 12
    cell = (N - 2 * margin - 3 * gap) // 4
    lit = {(0, 3), (1, 2), (1, 3), (2, 1), (2, 2), (3, 0), (3, 1)}  # diagonal-ish sweep
    for gy in range(4):
        for gx in range(4):
            x = margin + gx * (cell + gap)
            y = margin + gy * (cell + gap)
            if (gy, gx) in lit:
                # brightness ramps along the sweep for a sense of motion direction
                t = (gx + (3 - gy)) / 6.0
                color = (int(60 + 30 * t), int(180 + 70 * t), int(60 + 20 * t), 255)  # BGRA green
            else:
                color = (60, 55, 50, 255)
            sub = img[y:y + cell, x:x + cell]
            tile = rounded_mask(cell, cell, 10)
            sub[tile > 0] = color

    ok, png = cv2.imencode(".png", img)
    if not ok:
        raise SystemExit("png encode failed")
    png = png.tobytes()

    # minimal ICO: 1 PNG-encoded entry (Vista+). width/height byte 0 == 256.
    ICONDIR = struct.pack("<HHH", 0, 1, 1)
    ICONDIRENTRY = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(ICONDIR + ICONDIRENTRY + png)
    print(f"wrote {OUT}  ({len(png)} bytes PNG, 256x256)")


if __name__ == "__main__":
    main()

"""per-stage profiler: where does the MV-engine per-frame time actually go?
helps decide what (if anything) is worth moving to the GPU."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import av  # noqa: E402
import cv2  # noqa: E402
import numpy as np  # noqa: E402

from v2 import params as P  # noqa: E402
from v2.compose import Compositor  # noqa: E402
from v2.decode import Decoder  # noqa: E402
from v2.mask import MaskBuilder  # noqa: E402
from v2.motion import activity_from_mvs  # noqa: E402


def main(path: str, n: int = 300) -> None:
    pr = P.Params(); pr.engine = "mv"
    dec = Decoder(path); dec.open()
    mask = MaskBuilder(); comp = Compositor()
    fp = 1.0 / max(1.0, dec.fps)

    t = {k: 0.0 for k in ("read_demux", "to_rgb", "motion", "mask", "compose")}
    i = 0
    # split decode into demux+decode vs the rgb24 conversion (swscale)
    container = dec._container
    stream = dec._stream
    frames_iter = container.decode(stream)

    while i < n:
        t0 = time.perf_counter()
        try:
            frame = next(frames_iter)
        except StopIteration:
            break
        t1 = time.perf_counter(); t["read_demux"] += t1 - t0
        rgb = frame.to_ndarray(format="rgb24")
        t2 = time.perf_counter(); t["to_rgb"] += t2 - t1

        mvs = None
        for sd in frame.side_data:
            if "MOTION" in str(sd.type).upper():
                mvs = np.frombuffer(bytes(sd), dtype=__import__("v2.decode", fromlist=["MV_DTYPE"]).MV_DTYPE)
        h, w = rgb.shape[:2]
        act = activity_from_mvs(mvs, w, h, pr.mv_block_px, pr.mv_gain) if mvs is not None else \
            np.zeros((h // pr.mv_block_px, w // pr.mv_block_px), np.uint8)
        t3 = time.perf_counter(); t["motion"] += t3 - t2

        mg = mask.build(act, pr, fp, h)
        t4 = time.perf_counter(); t["mask"] += t4 - t3

        thr = max(1, int(pr.threshold * 255))
        _, motion = cv2.threshold(act, thr, 255, cv2.THRESH_BINARY)
        comp.composite(rgb, mg, motion, pr.tile_px, pr.alpha_floor)
        t5 = time.perf_counter(); t["compose"] += t5 - t4
        i += 1

    dec.close()
    print(f"\nper-stage over {i} frames (MV engine, {w}x{h}):")
    tot = 0.0
    for k, v in t.items():
        ms = v / i * 1000
        tot += ms
        print(f"  {k:12s} {ms:6.2f} ms")
    print(f"  {'TOTAL':12s} {tot:6.2f} ms   (decode read = read_demux+to_rgb = "
          f"{(t['read_demux']+t['to_rgb'])/i*1000:.2f} ms)")


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "The King of Queens - S04E01 - Walk, Man.mp4"
    main(src)

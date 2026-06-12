"""headless bench: run the real decode->motion->mask->compose pipeline (no Qt) and report
per-stage timing + dirty-tile counts, to validate the 'less work' thesis before the GUI."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np  # noqa: E402

from v2 import params as P  # noqa: E402
from v2.compose import Compositor  # noqa: E402
from v2.decode import Decoder  # noqa: E402
from v2.mask import MaskBuilder  # noqa: E402
from v2.motion import DiffActivity, activity_from_mvs, grid_dims  # noqa: E402

import cv2  # noqa: E402


def run(path: str, engine: str, n: int = 300, use_gpu: bool = False) -> None:
    pr = P.Params()
    pr.engine = engine
    pr.use_gpu = use_gpu
    dec = Decoder(path)
    dec.open()
    mask = MaskBuilder()
    diff = DiffActivity()
    comp = Compositor()
    fp = 1.0 / max(1.0, dec.fps)

    t_proc, dirty, active = [], [], []
    i = 0
    while i < n:
        f = dec.read()
        if f is None:
            dec.rewind(); mask.reset_persistence(); diff.reset(); continue
        t0 = time.perf_counter()
        use_diff = engine == P.ENGINE_DIFF or (engine == P.ENGINE_AUTO and f.mvs is None)
        if use_diff:
            act = diff.compute(f, pr.mv_block_px, pr.threshold)
        elif f.mvs is not None:
            act = activity_from_mvs(f.mvs, f.width, f.height, pr.mv_block_px, pr.mv_gain)
        else:
            gw, gh = grid_dims(f.width, f.height, pr.mv_block_px)
            act = np.zeros((gh, gw), np.uint8)
        mg = mask.build(act, pr, fp, f.height)
        thresh_u8 = max(1, int(pr.threshold * 255))
        _, motion = cv2.threshold(act, thresh_u8, 255, cv2.THRESH_BINARY)
        rects = comp.composite(f.rgb, mg, motion, pr.tile_px, pr.alpha_floor, pr.use_gpu)
        t_proc.append(time.perf_counter() - t0)
        dirty.append(len(rects))
        active.append(mask.last_active_frac)
        i += 1

    dec.close()
    Th = (dec.height + pr.tile_px - 1) // pr.tile_px
    Tw = (dec.width + pr.tile_px - 1) // pr.tile_px
    total_tiles = Th * Tw
    avg_ms = sum(t_proc) / len(t_proc) * 1000
    p95_ms = sorted(t_proc)[int(len(t_proc) * 0.95)] * 1000
    print(f"\nengine={engine}  gpu={use_gpu}  frames={len(t_proc)}")
    print(f"  proc: {avg_ms:.2f}ms avg / {p95_ms:.2f}ms p95")
    print(f"  dirty tiles: {sum(dirty)/len(dirty):.1f} avg / {total_tiles} total "
          f"({100*sum(dirty)/len(dirty)/total_tiles:.0f}% of frame touched)")
    print(f"  active: {100*sum(active)/len(active):.1f}% avg")


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "The King of Queens - S04E01 - Walk, Man.mp4"
    run(src, P.ENGINE_MV, use_gpu=False)
    run(src, P.ENGINE_MV, use_gpu=True)

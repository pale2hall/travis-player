"""isolate exactly the op GPU replaces — full-frame upscale+floor+premultiply — measured
per-frame on FRESH decoded frames (no warm-buffer reuse), CPU vs GPU, with transfer broken
out. tells us whether the round-trip kills the win and whether uploading YUV (half the bytes,
skips swscale) would change the verdict."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import warnings
import cv2  # noqa: E402
import numpy as np  # noqa: E402
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import cupy as cp  # noqa: E402
    from cupyx.scipy.ndimage import zoom as cp_zoom  # noqa: E402

from v2.decode import Decoder  # noqa: E402

FLOOR = 13


def cpu_premul(rgb, mask_grid):
    h, w = rgb.shape[:2]
    mf = cv2.resize(mask_grid, (w, h), interpolation=cv2.INTER_LINEAR)
    a = cv2.convertScaleAbs(mf, alpha=(255 - FLOOR) / 255.0, beta=FLOOR)
    a3 = cv2.merge([a, a, a])
    return cv2.multiply(rgb, a3, scale=1.0 / 255.0), a


def gpu_premul(rgb, mask_grid, t):
    h, w = rgb.shape[:2]; gh, gw = mask_grid.shape
    s = time.perf_counter()
    rg = cp.asarray(rgb); mg = cp.asarray(mask_grid)
    cp.cuda.Stream.null.synchronize(); t["upload"] += time.perf_counter() - s
    s = time.perf_counter()
    mf = cp_zoom(mg, (h / gh, w / gw), order=1, mode="nearest").astype(cp.uint16)
    a = (mf * (255 - FLOOR) // 255 + FLOOR).astype(cp.uint8)
    pm = ((rg.astype(cp.uint16) * a[:, :, None].astype(cp.uint16)) // 255).astype(cp.uint8)
    cp.cuda.Stream.null.synchronize(); t["compute"] += time.perf_counter() - s
    s = time.perf_counter()
    out = cp.asnumpy(pm); aa = cp.asnumpy(a)
    t["download"] += time.perf_counter() - s
    return out, aa


def main(path, n=200):
    dec = Decoder(path); dec.open()
    w, h = dec.width, dec.height
    gh, gw = h // 16, w // 16
    frames = []
    while len(frames) < n:
        f = dec.read()
        if f is None:
            dec.rewind(); continue
        frames.append(np.ascontiguousarray(f.rgb))
    dec.close()
    mask = (np.random.rand(gh, gw) * 255).astype(np.uint8)

    # warm gpu
    gpu_premul(frames[0], mask, {"upload": 0, "compute": 0, "download": 0})

    t0 = time.perf_counter()
    for rgb in frames:
        cpu_premul(rgb, mask)
    cpu_ms = (time.perf_counter() - t0) / n * 1000

    t = {"upload": 0.0, "compute": 0.0, "download": 0.0}
    t0 = time.perf_counter()
    for rgb in frames:
        gpu_premul(rgb, mask, t)
    gpu_ms = (time.perf_counter() - t0) / n * 1000

    print(f"\nfull-frame upscale+premul @ {w}x{h}, {n} fresh frames:")
    print(f"  CPU:  {cpu_ms:5.2f} ms")
    print(f"  GPU:  {gpu_ms:5.2f} ms   (upload {t['upload']/n*1000:.2f} + "
          f"compute {t['compute']/n*1000:.2f} + download {t['download']/n*1000:.2f})")
    print(f"  verdict: GPU is {'FASTER' if gpu_ms < cpu_ms else 'SLOWER'} by "
          f"{abs(cpu_ms-gpu_ms):.2f} ms")
    print(f"\n  note: RGB upload is {w*h*3/1e6:.1f}MB; a YUV420 upload would be "
          f"{w*h*1.5/1e6:.1f}MB and also skip the ~2.8ms CPU swscale.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "The King of Queens - S04E01 - Walk, Man.mp4")

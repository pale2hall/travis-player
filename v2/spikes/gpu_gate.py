"""GPU gate: does a kernel actually run on this (Blackwell/sm_120) card, and does a full
GPU composite — upload RGB + mask, upscale, floor, premultiply, download RGBA — beat the
~9.35ms CPU compose once PCIe transfer is included? build nothing until this passes."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np  # noqa: E402

try:
    import cupy as cp  # noqa: E402
    from cupyx.scipy.ndimage import zoom as cp_zoom  # noqa: E402
except Exception as e:  # noqa: BLE001
    print(f"FAIL: cupy import failed: {e!r}")
    sys.exit(1)


def smoke() -> None:
    dev = cp.cuda.Device(0)
    props = cp.cuda.runtime.getDeviceProperties(0)
    name = props["name"].decode()
    cc = f"{props['major']}.{props['minor']}"
    print(f"device: {name}  compute capability {cc}  ({dev.mem_info[1]//(1024**2)} MiB)")
    a = cp.arange(1_000_000, dtype=cp.float32)
    b = (a * 2.0 + 1.0).sum()
    cp.cuda.Stream.null.synchronize()
    exp = (np.arange(1_000_000, dtype=np.float32) * 2.0 + 1.0).sum()
    ok = abs(float(b) - float(exp)) / exp < 1e-4
    print(f"kernel correctness: {'OK' if ok else 'MISMATCH'}")
    if not ok:
        raise SystemExit("FAIL: kernel result wrong")


def gpu_composite(rgb_g, mask_g, w, h, floor_u8, scale):
    """mask_grid (gh,gw) uint8 → upscale to (h,w), apply floor, premultiply rgb → RGBA. on GPU."""
    gh, gw = mask_g.shape
    mask_full = cp_zoom(mask_g, (h / gh, w / gw), order=1, mode="nearest").astype(cp.uint16)
    alpha = (mask_full * (255 - floor_u8) // 255 + floor_u8).astype(cp.uint16)  # (h,w)
    rgba = cp.empty((h, w, 4), dtype=cp.uint8)
    a = alpha[:, :, None]
    rgba[:, :, 0:3] = ((rgb_g.astype(cp.uint16) * a) // 255).astype(cp.uint8)
    rgba[:, :, 3] = alpha.astype(cp.uint8)
    return rgba


def bench(path: str, n: int = 200) -> None:
    from v2.decode import Decoder
    dec = Decoder(path); dec.open()
    w, h = dec.width, dec.height
    # grab one representative frame's rgb + a synthetic mask grid
    f = None
    while f is None:
        f = dec.read()
    gh, gw = h // 16, w // 16
    rgb = np.ascontiguousarray(f.rgb)
    mask_grid = (np.random.rand(gh, gw) * 255).astype(np.uint8)
    dec.close()

    floor_u8, scale = 13, 1.0
    # warm up (first kernel launch + zoom compile)
    rg = cp.asarray(rgb); mg = cp.asarray(mask_grid)
    _ = gpu_composite(rg, mg, w, h, floor_u8, scale)
    cp.cuda.Stream.null.synchronize()

    # full round trip incl transfers
    t0 = time.perf_counter()
    for _ in range(n):
        rg = cp.asarray(rgb)
        mg = cp.asarray(mask_grid)
        out = gpu_composite(rg, mg, w, h, floor_u8, scale)
        _ = cp.asnumpy(out)
    cp.cuda.Stream.null.synchronize()
    full_ms = (time.perf_counter() - t0) / n * 1000

    # compute only (data already on GPU)
    t0 = time.perf_counter()
    for _ in range(n):
        out = gpu_composite(rg, mg, w, h, floor_u8, scale)
    cp.cuda.Stream.null.synchronize()
    compute_ms = (time.perf_counter() - t0) / n * 1000

    print(f"\nGPU composite @ {w}x{h}, {n} iters:")
    print(f"  full round-trip (upload+compute+download): {full_ms:6.2f} ms")
    print(f"  compute only (data resident on GPU):       {compute_ms:6.2f} ms")
    print(f"  CPU compose baseline (from profiler):        ~9.35 ms")
    verdict = full_ms < 9.35
    print(f"\nGATE: {'PASS — GPU composite beats CPU' if verdict else 'MARGINAL — transfer dominates'}")


if __name__ == "__main__":
    smoke()
    src = sys.argv[1] if len(sys.argv) > 1 else "The King of Queens - S04E01 - Walk, Man.mp4"
    bench(src)

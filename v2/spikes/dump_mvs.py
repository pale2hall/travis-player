"""phase-0 gate: confirm PyAV exports usable h.264 motion vectors on real content.

this is the single assumption the whole v2 plan rests on. v1's mpv-shader route died
because a cross-frame-state assumption was never verified before building on it. so before
writing any pipeline, we prove here that:

  1. PyAV can be told to export motion vectors (the `export_mvs` codec flag),
  2. real P-frames in the bundled clip actually carry a non-trivial number of them,
  3. the vectors spatially track on-screen motion (eyeball via heatmap PNGs).

run:
  .venv/Scripts/python v2/spikes/dump_mvs.py "King of Queens - S04E01 - Walk, Man.mp4"

outputs:
  - per-pict-type MV-count stats to stdout
  - a handful of MV-magnitude heatmap PNGs under v2/spikes/out/ for visual confirmation
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import av
import cv2
import numpy as np

OUT_DIR = Path(__file__).resolve().parent / "out"
N_FRAMES = 600          # sample budget — enough to span several GOPs + a scene or two
N_HEATMAPS = 6          # how many P-frame heatmaps to dump
HEATMAP_EVERY = 40      # space the dumped heatmaps out across the sample


def _open_with_mvs(path: str) -> tuple[av.container.InputContainer, av.video.stream.VideoStream]:
    """Open the container and enable motion-vector export on the video stream's
    codec context BEFORE any frame is decoded (the flag is read at decode time)."""
    container = av.open(path)
    stream = container.streams.video[0]
    # the working incantation: set the export_mvs codec flag. PyAV exposes ffmpeg's
    # `flags2 +export_mvs` via codec_context.options applied before decoding.
    cc = stream.codec_context
    try:
        cc.options = {"flags2": "+export_mvs"}
    except Exception as e:  # noqa: BLE001 — spike: report and continue to see what happens
        print(f"  ! setting flags2 via options failed: {e!r}")
    return container, stream


def _mv_array(frame: av.VideoFrame):
    """Return the MOTION_VECTORS side-data as a numpy structured array, or None."""
    for sd in frame.side_data:
        # side-data type name is 'MOTION_VECTORS' in PyAV
        if "MOTION" in str(sd.type).upper():
            try:
                return np.frombuffer(bytes(sd), dtype=_MV_DTYPE)
            except Exception as e:  # noqa: BLE001
                print(f"  ! could not parse MV side data: {e!r}")
                return None
    return None


# ffmpeg AVMotionVector layout (libavutil/motion_vector.h), little-endian.
# align=True reproduces the C natural alignment: a 2-byte pad before the
# uint64 `flags` and a 6-byte tail pad → 40 bytes total.
#   int32 source; uint8 w, h; int16 src_x, src_y, dst_x, dst_y;
#   uint64 flags; int32 motion_x, motion_y; uint16 motion_scale
_MV_DTYPE = np.dtype([
    ("source", "<i4"),
    ("w", "u1"), ("h", "u1"),
    ("src_x", "<i2"), ("src_y", "<i2"),
    ("dst_x", "<i2"), ("dst_y", "<i2"),
    ("flags", "<u8"),
    ("motion_x", "<i4"), ("motion_y", "<i4"),
    ("motion_scale", "<u2"),
], align=True)


def _save_heatmap(path: Path, mvs, width: int, height: int, block: int = 16) -> None:
    """Render an MV-magnitude heatmap at macroblock resolution and write a PNG.
    Uses PyAV's own encoder so the spike needs no extra image deps."""
    gw, gh = max(1, width // block), max(1, height // block)
    grid = np.zeros((gh, gw), dtype=np.float32)
    for mv in mvs:
        scale = max(1, int(mv["motion_scale"]))
        mag = (float(mv["motion_x"]) ** 2 + float(mv["motion_y"]) ** 2) ** 0.5 / scale
        gx = min(gw - 1, max(0, int(mv["dst_x"]) // block))
        gy = min(gh - 1, max(0, int(mv["dst_y"]) // block))
        grid[gy, gx] = max(grid[gy, gx], mag)

    if grid.max() > 0:
        norm = (grid / grid.max() * 255.0).astype(np.uint8)
    else:
        norm = grid.astype(np.uint8)
    # nearest-neighbour upscale to a viewable size, apply a colormap for legibility
    up = np.repeat(np.repeat(norm, block, axis=0), block, axis=1)
    heat = cv2.applyColorMap(up, cv2.COLORMAP_INFERNO)
    cv2.imwrite(str(path), heat)


def main() -> int:
    if len(sys.argv) < 2:
        # default to the bundled clip if present
        default = Path(__file__).resolve().parents[2] / "King of Queens - S04E01 - Walk, Man.mp4"
        if not default.exists():
            print("usage: dump_mvs.py <video>", file=sys.stderr)
            return 2
        path = str(default)
    else:
        path = sys.argv[1]

    print(f"opening: {path}")
    container, stream = _open_with_mvs(path)
    w = stream.codec_context.width
    h = stream.codec_context.height
    print(f"video: {w}x{h}  codec={stream.codec_context.name}")
    print(f"MV struct size = {_MV_DTYPE.itemsize} bytes (expect 40)")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    counts_by_type: dict[str, list[int]] = defaultdict(list)
    frames_with_mvs = 0
    heatmaps_dumped = 0

    for i, frame in enumerate(container.decode(stream)):
        if i >= N_FRAMES:
            break
        # PyAV 17 returns pict_type as a plain int enum value
        ptype = {0: "NONE", 1: "I", 2: "P", 3: "B"}.get(int(frame.pict_type), str(int(frame.pict_type)))
        mvs = _mv_array(frame)
        n = 0 if mvs is None else len(mvs)
        counts_by_type[ptype].append(n)
        if n > 0:
            frames_with_mvs += 1
            if heatmaps_dumped < N_HEATMAPS and i % HEATMAP_EVERY == 0:
                out = OUT_DIR / f"mv_{i:04d}_{ptype}.png"
                _save_heatmap(out, mvs, w, h)
                heatmaps_dumped += 1
                print(f"  frame {i:4d} [{ptype}]  {n:5d} MVs  → {out.name}")

    container.close()

    print("\n-- summary --")
    total = sum(len(v) for v in counts_by_type.values())
    print(f"decoded {total} frames, {frames_with_mvs} carried motion vectors")
    for ptype in sorted(counts_by_type):
        vals = counts_by_type[ptype]
        nz = [v for v in vals if v > 0]
        avg = (sum(vals) / len(vals)) if vals else 0
        print(f"  {ptype}-frames: {len(vals):4d}   "
              f"avg {avg:7.1f} MVs   {len(nz)}/{len(vals)} non-empty   "
              f"max {max(vals) if vals else 0}")

    verdict = any(v > 0 for v in counts_by_type.get("P", []))
    print(f"\nGATE: {'PASS — PyAV exports usable MVs, proceed to phase 1' if verdict else 'FAIL — no MVs, revisit plan'}")
    print(f"heatmaps: {OUT_DIR}")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())

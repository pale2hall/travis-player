# Implementation notes

The other docs in `notes/` (`plan.md`, `mpv-hybrid-build-plan.md`,
`mpv-route-rubric.md`, `build-status.md`) describe **Route A**: the original
plan to fork mpv and add transparency in C against the render path. That route
was abandoned partway through Phase H1 — see "What we tried first" below.

## What we tried first (and why it failed)

We attempted the GLSL-hook approach using mpv's user-shader system:

1. Pre-built mpv 0.36 + custom `motion_mask.glsl` with two `//!HOOK OUTPUT`
   passes — one to compare current vs `TRAVIS_PREV`, one to save the current
   frame as `TRAVIS_PREV` for next frame
2. Lua control script writing `#define`s into the shader file and reloading

It did not work because mpv's shader scheduler is dependency-driven within a
single frame: a hook that *produces* `TRAVIS_PREV` is run before any hook that
*consumes* it. With both hooks at the same render stage, this means the
"previous frame" texture always ended up holding the **current** frame's data
— diff was always zero, mask was always blue (no motion). Single-hook
`BIND TRAVIS_PREV / SAVE TRAVIS_PREV` was silently skipped on the first
frame because the texture didn't exist yet, and never bootstrapped.

mpv's `//!SAVE` / `//!BIND` are designed for within-frame intermediate
computation, not cross-frame state. Doing it properly would require either
forking the mpv C source, using `vf=lavfi=tblend` (CPU-side, defeats the
point), or moving the whole pipeline elsewhere.

The shaders/lua/PowerShell-build artifacts from that attempt are kept under
`shaders/` and `scripts/` for reference but are no longer used by the
running app.

## Where we ended up

A Python application (`travis_player.py`) using:

- **PyQt6** — frameless `WA_TranslucentBackground` window for real OS-level
  per-pixel alpha (which pure mpv on Windows can't do regardless)
- **OpenCV** — `cv2.VideoCapture` for decode, `cv2.absdiff` / `Sobel` /
  `connectedComponentsWithStats` / `dilate` / `GaussianBlur` / `LUT` for the
  mask pipeline. All `uint8` ops where possible
- **mpv as audio sidecar** — separate process with `--no-video`, controlled
  via Windows named-pipe IPC for pause/resume. Wrapped in a Job Object so
  it cannot orphan when the parent dies
- **NumPy** — only for the few cases where `cv2` doesn't have a primitive

### Per-frame compute budget at 1080p

Measured on Ryzen 9 9950X3D + RTX 5090 (decode is single-core CPU-bound,
GPU is unused):

| stage                                | time   |
|--------------------------------------|--------|
| `cv2.VideoCapture.read` (h264 1080p) | ~6 ms  |
| BGR→gray + downscale to 1/8          | ~1 ms  |
| `cv2.absdiff` + threshold            | <1 ms  |
| Sobel edge weighting (when on)       | ~2 ms  |
| Connected components (when min>0)    | ~2 ms  |
| Persistence (running max + decay)    | <1 ms  |
| Dilate + gaussian blur on mask       | <1 ms  |
| Mask upscale + RGBA composite        | ~3 ms  |
| `QImage.copy()`                      | ~2 ms  |
| **total**                            | ~11 ms |

23.98 fps target = 41.7 ms per-frame budget. Plenty of headroom even with
all the optional passes turned on.

### The pixel-scaling trick

Region-shape parameters (padding, feather, min-region-size) are exposed to the
user as **"pixels at 1080p reference"**. Internally:

```python
def _ref_to_proc_px(self, ref_px_1080: int) -> int:
    scale = video_height / 1080.0
    actual_video_px = ref_px_1080 * scale
    return max(1, round(actual_video_px / proc_div))
```

So `feather=8` at 1080p with proc_div=8 → 1 proc-pixel kernel. At 4K with
the same settings → 2 proc-pixel kernel. The visual feel stays consistent
across resolutions without the user having to touch sliders.

### Why an audio sidecar instead of bundling audio in Python

PyAV / sounddevice would let us decode audio in-process, but:
- Audio playback timing is a solved problem in mpv
- The IPC pause command is one named-pipe write
- The Job Object pattern handles cleanup robustly
- Decode timing on the audio path doesn't affect video processing

Cost: pause/resume is sub-frame-accurate but seek/scrub aren't synced
between the video and audio paths. Acceptable for a prototype.

## Open questions / next steps

- True motion-vector access from H264 streams via PyAV would replace the
  pixel-diff with even cheaper (and more accurate, since they're literally
  the encoder's own motion estimates) mask. Worth trying if perf becomes a
  ceiling.
- Audio drift over long playback — if it becomes noticeable, periodically
  re-sync via mpv IPC `seek` to match `cv2.CAP_PROP_POS_MSEC`.
- 4K HDR sources would need a different pixel format path. Not in scope yet.

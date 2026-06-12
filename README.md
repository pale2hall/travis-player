# travis-player

**A video player where only the motion is visible.** Static regions of the frame
fade to actual OS-level transparency — you see your desktop through the parts of
the video where nothing is happening. The moving parts stay opaque and play
normally, audio and all.

And since v2, it does this by reading **the motion vectors already inside the
video file** — the ones the encoder computed when the video was compressed —
instead of computing motion itself.

![intro shot showing the King of Queens intro composited over Task Manager](notes/screenshot.png)

## The idea

A compressed video is not a stack of pictures. It's one picture followed by
instructions: *this 16×16 block didn't change; this block moved 3px left; this
block is new.* Every h.264 P-frame is a per-macroblock map of exactly where the
motion is — computed once, by the encoder, possibly years ago, and shipped
inside every copy of the file.

Normal players decode all of that into flat RGB and throw the map away.
v1 of this project did too — then spent CPU re-deriving a worse version of it
by diffing consecutive frames. Every frame. Even when the scene was a static
sitcom kitchen for 22 minutes.

v2 keeps the map:

1. **Decode with motion vectors exported** (PyAV / FFmpeg `flags2 +export_mvs`).
   ~7,800 vectors per P-frame on 1080p TV content. Skip-coded blocks — the ones
   the encoder didn't even bother re-sending — are *guaranteed static* and cost
   nothing.
2. **Scatter MV magnitudes into a macroblock grid** (~120×68 cells for 1080p).
   That tiny grid is the activity mask. No full-frame pixel work.
3. **Shape it** — threshold, persistence with decay, dilation, feather, falloff
   curve — all at grid resolution, where it's effectively free.
4. **Composite only the tiles that visibly changed** into a persistent RGBA
   buffer, and repaint only those rects. A fully static scene composites
   *zero* tiles and repaints *nothing*.

### The numbers (Ryzen 9 9950X3D, 1080p 23.976fps)

| scene | v1 (full-frame pixel diff) | v2 (codec-driven) |
|---|---|---|
| motion-heavy intro | ~11 ms/frame, every frame | ~18 ms (everything's moving — fair) |
| typical dialogue | ~11 ms/frame, every frame | 3–10 ms, dozens of tiles |
| static scene | ~11 ms/frame, every frame | **~2.8 ms, 0 tiles repainted** |

The player's cost now scales with *how much is actually happening on screen*,
which was the whole point.

### The RTX 5090 plot twist

We wired the compositing math to the GPU (CuPy/CUDA, works fine on Blackwell)
and it was… a wash. Profiling showed the per-frame math is ~3 ms on CPU and the
PCIe round-trip costs more than the compute. This workload is memory-bound, not
compute-bound — a 5090 has nothing to chew on at 1080p. The GPU backend ships
anyway (`Engine → GPU compositing`, default off) because at 4K+ the math should
flip the verdict. The receipts are in `v2/spikes/gpu_truth.py`.

The honest GPU win — an end-to-end GPU display path with no CPU readback — is
deliberately deferred. NVDEC hardware decode was ruled out on purpose: it's
fixed-function and *strips the motion vectors*, which would delete the thesis.

## Watch it think

The control panel ships debug views that make the pipeline visible:

- **MV / activity heatmap** — the codec's own motion-vector field, live
- **Dirty-tile map** — exactly which tiles the compositor repaints (and which
  it skips) each frame
- **Binary mask** — the stark what-counts-as-motion silhouette

Load preset `06 xray` then `07 tile inspector` and you're watching the encoder's
20-year-old homework drive a real-time transparency effect.

## Run it

Requires Python 3.10+ and `mpv` on PATH (audio sidecar).

```powershell
git clone <this repo> ; cd travis-player
python -m venv .venv
.venv\Scripts\pip install PyQt6 opencv-python numpy av
# optional GPU backend:  .venv\Scripts\pip install "cupy-cuda12x[ctk]"
```

**Desktop shortcut** (recommended) — creates a `travis-player` icon on your Desktop
that opens the app empty (no console window):

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_shortcut.ps1
```

Double-click it, then **drag a video file or stream URL onto the window**. That's it.

Or from a terminal:

```powershell
.\launch_v2.ps1                                     # opens empty, drag a video in
.\launch_v2.ps1 "path\to\video.mp4"                 # autoplay a file
.\launch_v2.ps1 "https://example.com/stream.m3u8"   # streams work too
```

Or directly: `.venv\Scripts\python -m v2` (add a path/URL to autoplay one).

Drag-and-drop a file or a stream URL onto either window any time to swap sources live.

### Controls

| key / mouse | action |
|---|---|
| Space | pause / resume |
| F / F11 / double-click | fullscreen |
| R | reset window geometry |
| Q / Esc | quit |
| drag top bar / edges | move / resize the frameless window |
| hover bottom edge | seek + volume bars |

The control panel has live sliders for everything (threshold, feather,
persistence, falloff, …), **Auto** buttons that derive block/tile sizes from
the current stream's codec and resolution, and 18 curated presets — from
`01 ghost` (pure motion) through `05 halo bloom` to `18 strobe`. Each preset
JSON carries a `_note` explaining what it's for.

## Architecture (v2)

```
v2/
  decode.py    PyAV decoder, export_mvs on → RGB + motion vectors + pts
  motion.py    MVs → macroblock activity grid (pixel-diff fallback for I-frames)
  mask.py      grid-resolution shaping: threshold/persist/dilate/feather/falloff
  compose.py   dirty-tile compositor — persistent RGBA buffer, visibility-gated
  clock.py     master clock: video chases wall time, drops to catch up (A/V lock)
  gpu.py       optional CuPy compositing backend (auto CPU fallback)
  worker.py    QThread orchestration + perf telemetry
  player.py    frameless translucent window, dirty-rect repaint
  controls.py  live-tuning panel, presets, debug views, stream-aware Auto
  audio.py     mpv --no-video sidecar over a named pipe, Job-Object lifetime
  spikes/      the receipts: MV-export gate, per-stage profiler, GPU benchmarks
```

Three decisions carry the design:

- **Visibility-gated dirty detection.** Real-world rips code ~96% of macroblocks
  every frame (film grain), so "has a motion vector" can't mean "needs repaint."
  A tile recomposites only if its alpha changed *or* it has motion **and** is
  actually visible. A ghost at 5% alpha doesn't get refreshed — you can't see it.
- **Change detection runs at grid resolution.** A static frame never touches a
  full-resolution buffer at all.
- **One clock.** Video presents against wall time (which is what mpv's audio
  follows) and drops frames to stay honest. v1 paced by sleeping a frame period
  and accumulated drift; v2 can't.

## History: v1

`travis_player.py` is the previous generation — same effect, computed the hard
way (`cv2.VideoCapture` + full-frame diff at 1/8 resolution + per-frame full
RGBA rebuild). It works and stays runnable (`.\launch.ps1`), and its
`notes/implementation.md` documents the journey, including the failed first
attempt to do this inside mpv's GLSL shader hooks (cross-frame state isn't a
thing there — the post-mortem is in the notes).

## License / provenance

An art-piece-slash-experiment about how much of a video player's work is
already done by the file it's playing. Built with PyQt6, PyAV, OpenCV, NumPy,
mpv — and an unreasonable amount of profiling.

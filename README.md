# travis-player

A motion-aware video player with per-pixel transparency. Static regions of the
video fade to transparent so you can see your desktop / other apps through them,
while regions where things are actually moving stay opaque. The threshold,
softness, persistence, region-shape rules, and noise-rejection are all tunable
live via a separate control panel.

![intro shot showing the King of Queens intro composited over Task Manager](notes/screenshot.png)

## Why this exists

Most of the pixels in any given video frame don't change frame-to-frame. The
framing on a sitcom is mostly the same kitchen for 22 minutes. The point of
this player is to figure out — cheaply — which pixels are actually carrying
motion information and let everything else get out of the way.

## Architecture

```
┌──────────────────────┐     QImage     ┌──────────────────────┐
│  FrameWorker         │  ───signal───▶ │  PlayerWindow        │
│  (QThread)           │                │  (PyQt6 frameless    │
│                      │                │   translucent)       │
│  cv2.VideoCapture    │                │                      │
│  → BGR frame         │                │  paints RGBA at      │
│  → diff @ proc res   │                │   OS-level alpha     │
│  → activity mask     │                │                      │
│  → RGBA composite    │                │                      │
└──────────────────────┘                └──────────────────────┘
                                                  │
                                          shared Params
                                                  │
                                        ┌─────────▼──────────┐
                                        │  ControlPanel      │
                                        │  (sliders, persist │
                                        │   to settings.json)│
                                        └────────────────────┘

         ┌─────────────────────────────────────────────────┐
         │  AudioController                                │
         │  → mpv subprocess (--no-video)                  │
         │  → IPC named pipe for pause/resume              │
         │  → Windows Job Object so it dies with parent    │
         └─────────────────────────────────────────────────┘
```

### The motion-mask pipeline (per frame)

1. **Decode** with `cv2.VideoCapture` → BGR frame at full video resolution
2. **Downscale** to `1/proc_div` per axis for the diff math (cheap)
3. **Frame diff** vs previous downsampled luma → raw activity mask
4. **Edge sensitivity** — multiply by Sobel-derived edge magnitude so
   compression noise on flat backgrounds is suppressed
5. **Scene-cut rejection** — if active fraction > threshold, drop this
   frame's diff (cuts and brightness flashes look like motion-everywhere
   but aren't)
6. **Min-region filter** — `cv2.connectedComponentsWithStats` rejects
   blobs smaller than the configured size (kills speckle/grain)
7. **Persistence** — running max with linear decay, so areas that moved
   recently stay visible for N seconds before fading
8. **Padding** — `cv2.dilate` to expand the mask outward, keeping
   fast-moving edges fully opaque
9. **Feather** — gaussian blur for soft transitions
10. **Falloff curve** — power LUT shaping the active→static ramp
11. **Upscale** to full video resolution, build the RGBA output (premultiplied),
    deep-copy the QImage so the worker can reuse the buffer next frame

All region-size parameters are specified as **pixels at 1080p reference** and
auto-scale based on the actual video height (`_ref_to_proc_px`), so the same
slider value gives the same visual feel on a 480p clip and a 4K stream.

### Why this and not the original mpv-fork plan

The original plan in `notes/plan.md` was Route A: fork mpv and add the
transparency pipeline in C against the render path. We tried it (see
`shaders/` which is now obsolete) and found mpv 0.36's GLSL hook system
schedules `//!SAVE` and `//!BIND` on the same texture so that within one frame
the save can run before the bind — meaning frame-to-frame state isn't
achievable without forking the C source. The MSYS2 build environment also
isn't on this machine. Switching to a Python pipeline gave us proper frame
history AND real OS-level window transparency (PyQt6 `WA_TranslucentBackground`)
that pure mpv can't do alone, in a fraction of the engineering hours.

## Setup

Requires Python 3.10+ (this dev box has 3.13) and `mpv` on PATH for audio.

```powershell
python -m venv .venv
.venv\Scripts\pip install PyQt6 opencv-python numpy
```

## Run

```powershell
.\launch.ps1                                # uses first video file in this dir
.\launch.ps1 "path\to\video.mp4"
.\launch.ps1 "https://example.com/stream.m3u8"
.\launch.ps1 -Console                       # keep console for python errors
```

The launcher runs `pythonw.exe` detached and tails the log file briefly to
catch immediate-exit failures.

## Controls

### Player window
| key             | action                              |
|-----------------|-------------------------------------|
| Space           | pause/resume (audio + video)        |
| F11 / dbl-click | toggle fullscreen                   |
| R               | reset window size + position        |
| Q / Esc         | quit                                |
| drag (top bar)  | move window                         |
| drag (corners/edges) | resize                         |
| drop file/url   | swap source                         |

The chrome (top drag bar + bottom-right resize grip) appears on hover.

### Control panel

Same `R` key resets both windows' geometry. Sliders are organized into:

- **View** — debug mode (normal / heatmap / binary mask / off)
- **Visibility** — alpha floor for static regions
- **Motion detection** — threshold, edge sensitivity, min region size
- **Region shape** — padding, feather, feather falloff curve
- **Temporal** — persistence, scene-cut ignore + threshold
- **Compute** — proc divisor

Every slider has a tooltip explaining what it does.

## What gets persisted

`settings.json` (project dir) auto-saves on every change and on app close:

- All tunable parameters
- Both windows' size and position

Old settings files with renamed fields are silently ignored — defaults take over.

## Presets

The control panel has a Presets group at the top. Type a name and hit Save
to write `presets/<name>.json` (just the look-tuning fields — window
geometry isn't included so loading a preset doesn't move your windows).
The dropdown re-scans the `presets/` folder every time you open it, so
deleting a preset is just `del presets\old.json` outside the app.

Loading a preset also writes those values to `settings.json`, so the
loaded look is the new running default if you quit and relaunch.

## Logs

`logs/travis.log` (rotating, 3 × 2MB). Captures lifecycle, perf stats every
60 frames (`proc=11.3ms avg, real_fps=22.8, late=1/60`), audio sidecar pid,
and uncaught exceptions via `sys.excepthook`.

## Known limitations

- Audio sync is approximate — the audio sidecar mpv runs independently
  and may drift over long playback. Pause/resume is honored via IPC.
- 1080p processing comfortably runs at 23.98fps on a Ryzen 9 9950X3D.
  4K may need `proc_div ≥ 16`.
- Drag-drop loads via `cv2.VideoCapture` which uses FFmpeg under the
  hood — most stream protocols work (HTTP, RTSP, HLS) but exotic
  formats may not.

## Project layout

```
travis_player.py          main app (worker, player, controls, audio)
launch.ps1                detached launcher with health check
settings.json             auto-saved params + window geometry
logs/travis.log           rotating runtime log
notes/                    planning + design docs
shaders/                  obsolete mpv GLSL approach (kept for reference)
scripts/                  obsolete mpv lua + build scripts
```

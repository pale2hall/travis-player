# v2 design notes — codec-driven pipeline

v1 (`travis_player.py`, see `implementation.md`) proved the effect but re-derived
motion by pixel-diffing every decoded frame. v2's premise: the codec already did
motion estimation at encode time; read it instead of recomputing it.

## Gate-driven development

v1's first attempt (mpv GLSL hooks) died on an unverified assumption about
cross-frame state. v2 was built gate-first — each risky claim got a spike script
that had to pass before anything was built on it. They live in `v2/spikes/`:

| spike | claim tested | verdict |
|---|---|---|
| `dump_mvs.py` | PyAV exports usable h264 MVs on real content | PASS — ~7.8k MVs/P-frame, 575/575 non-empty, heatmaps track motion |
| `bench_pipeline.py` | MV pipeline beats v1's per-frame cost | PASS — 9.5ms vs 11ms on motion-heavy content, ~0 on static |
| `profile_stages.py` | where does frame time actually go? | compose 9.4ms, to_rgb 2.8ms, decode 0.2ms, mask ~0 |
| `gpu_gate.py` / `gpu_truth.py` | GPU compositing wins on a 5090 | **FAIL at 1080p** — transfer-bound (see below) |

## Things we learned the hard way

### Raw MV presence ≠ visible change

The first dirty-tile detector flagged a tile dirty if any motion vector landed
in it. On a real DVD rip, ~96% of macroblocks are coded every frame — film
grain gives nearly every block a ±1px vector. 97% of tiles repainted; slower
than v1. The fix is two gates:

- threshold MV magnitude before it counts as "content changed", and
- only repaint a moving tile if it's **visible** (alpha above a floor) —
  a 5%-alpha background ghost doesn't need refreshing.

With both, static scenes hit 0 dirty tiles / ~2.8ms.

### The decode struct is fiddly

FFmpeg's `AVMotionVector` unpacks as a 40-byte numpy dtype only with
`align=True` (2-byte pad before the `uint64 flags`, 6-byte tail). PyAV 17
returns `frame.pict_type` as a plain int (1=I, 2=P, 3=B), not an enum.
The export flag must be set on the codec context **before** the decode loop:
`stream.codec_context.options = {"flags2": "+export_mvs"}`.

### A 5090 can lose to a Ryzen

The compositing math (mask upscale + premultiply) is ~2.9ms on CPU. On the GPU
the same math is 0.4ms — but upload + download cost ~2ms, so end-to-end it's a
wash (2.5ms). The workload is memory-bandwidth-bound; there is no arithmetic
for 21,760 CUDA cores to win on. GPU backend kept (default off) for 4K+, where
the math scales up but the verdict needs re-measuring.

NVDEC was rejected outright: decode is 0.18ms (nothing to save) and hardware
decode **strips the motion vectors** — it would delete the premise.

### Sync by construction beats sync by correction

v1 paced video with `time.sleep(frame_period)` — error accumulates, audio (mpv,
real-time) drifts away. v2 presents each frame against a wall-clock master and
drops frames when behind. No drift to correct, so the planned IPC resync-nudge
was never needed.

## Deferred (deliberately)

- **End-to-end GPU display path** (QOpenGL/QRhi, zero CPU readback) — the real
  GPU win, but per-pixel window transparency + GL on Windows is a project of
  its own. Revisit for 4K.
- **In-process audio with a shared PTS clock** (drop the mpv sidecar).
- **HEVC/AV1 MV semantics** — `export_mvs` is h264/mpeg-family; modern codecs
  need their own extraction path (or the pixel-diff fallback, which already
  engages automatically).

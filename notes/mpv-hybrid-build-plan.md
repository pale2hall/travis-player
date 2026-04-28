# MPV Fork + Hybrid Plan (Execution Details)

## Why this path
- mpv gives us robust playback primitives immediately.
- Hybridization lowers long-term risk by keeping custom effect logic modular.
- We optimize for the fastest path to a compelling visual prototype on Windows 11.

## Architecture (MVP)

### Layer 1: Playback Core (mpv fork)
- Input demux + decode managed by mpv stack.
- Use existing playback lifecycle for pause/seek/audio/subtitles.

### Layer 2: Motion/Activity Module (new)
- Start with frame differencing between consecutive frames.
- Convert to tiled activity map (e.g., 16x16 or 32x32 blocks).
- Add temporal smoothing to reduce flicker.
- Add scene-cut detection to avoid global flash artifacts.

### Layer 3: Transparency Composer (new)
- Build alpha mask from activity map.
- Active regions: near-opaque.
- Static regions: fade to configurable alpha floor.
- Apply blur/feather to mask boundaries for visual softness.

### Layer 4: Control/Debug Surface
- Runtime parameters:
  - alpha_floor
  - activity_threshold
  - decay_rate
  - blur_radius
- Debug overlay modes:
  - activity heatmap
  - binary mask
  - final alpha mask

## Hybrid guardrails
To keep future migration possible:
1. Keep motion+alpha logic independent from player-specific APIs where practical.
2. Centralize tunable parameters in a config schema.
3. Define a small adapter boundary between mpv frame hooks and effect pipeline.

## Build order (concrete)
1. Confirm fork builds on Windows 11 with a repeatable command sequence.
2. Add frame tap hook and capture frame timing metrics.
3. Implement frame-diff activity map.
4. Implement alpha mask + compositor output.
5. Add runtime controls and debug overlay toggles.
6. Add settings persistence.
7. Run perf/stability pass across test clips and re-score rubric.

## Test clip pack (must-have)
- Low-motion talking head.
- High-motion sports/action.
- Grain/noise-heavy footage.
- Hard scene-cut montage.
- Mixed brightness/contrast content.

## Success metrics
- Average and p99 frame time at 1080p.
- CPU and GPU utilization on representative hardware.
- Number of visual artifacts per minute (flicker/halo/flash).
- Subjective visual score from quick review sessions.

## Known risks + mitigations
- **Risk:** Render-path complexity inside mpv fork.  
  **Mitigation:** Keep effect pipeline isolated and incrementally integrated.

- **Risk:** Flicker from naive frame differencing.  
  **Mitigation:** Temporal smoothing + adaptive thresholds + scene-cut resets.

- **Risk:** Product UX ceiling in pure mpv UI.  
  **Mitigation:** Preserve optional wrapper/app-shell phase as hybrid fallback.

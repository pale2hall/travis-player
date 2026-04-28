# Windows-first MVP plan

## Chosen direction
You selected: **Route A (mpv fork)** with a **hybrid execution style** to de-risk delivery.

That means:
- We use mpv as the playback foundation now.
- We isolate transparency/motion logic as a modular layer so we can evolve without destabilizing playback.
- We keep an escape hatch to a broader app-shell architecture later if product UX requires it.

## MVP outcome targets
1. Stable local video playback on Windows 11.
2. Realtime activity masking (frame differencing first).
3. Region-based transparency (active areas visible, static areas faded).
4. Interactive controls for threshold, alpha floor, smoothing, blur/feather.

## Hybrid phases
- **Phase H0 (1-2 days):** Fork setup + build reproducibility + baseline playback verification.
- **Phase H1 (3-5 days):** Motion-mask prototype in mpv render path (frame differencing).
- **Phase H2 (3-5 days):** Transparency controls + debug overlay + presets.
- **Phase H3 (1-2 weeks):** Performance pass, scene-cut handling, crash hardening.
- **Phase H4 (optional):** App-shell wrapper if UX/productization needs outgrow native mpv UX.

## Exit criteria for MVP alpha
- 1080p playback remains smooth on target test hardware.
- Transparency effect is stable (no severe flashing/ghosting in normal clips).
- Settings persist and can be tuned live.
- Basic diagnostics available (frame time + CPU/GPU usage).

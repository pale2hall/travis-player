"""generate a showcase set of presets into v2/presets/.

each preset is a full, coherent look that pushes one feature or extreme. a "_note" field
documents the intent; load_preset ignores unknown keys so it's harmless. re-runnable.

run:  .venv/Scripts/python v2/spikes/make_presets.py
"""

from __future__ import annotations

import json
from pathlib import Path

PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"

# the field set presets carry (mirrors Params minus geometry/volume/transient)
BASE = dict(
    engine="auto", mv_block_px=16, mv_gain=28.0, tile_px=64, iframe_hold=True,
    alpha_floor=0.05, threshold=0.04,
    padding_px_1080=0, feather_px_1080=8, feather_falloff=1.0,
    min_region_px_1080=0, push_full=0,
    persist_seconds=0.5, scene_cut_ignore=True, scene_cut_thresh=0.7,
    proc_div=8, debug_mode=0,
    resync_interval_s=5.0, resync_threshold_ms=120,
)


def preset(note: str, **over) -> dict:
    d = dict(BASE)
    d.update(over)
    d["_note"] = note
    return d


# name → overrides. names are zero-padded so the dropdown sorts into a tour order.
PRESETS = {
    "01 ghost (motion only)": preset(
        "only moving things appear, crisp, no trails. the classic look.",
        alpha_floor=0.0, threshold=0.06, mv_gain=24, persist_seconds=0.0,
        feather_px_1080=4, feather_falloff=1.6, push_full=30),

    "02 motion trails": preset(
        "long soft trails smear behind movement (high persistence + soft feather).",
        alpha_floor=0.0, persist_seconds=3.0, feather_px_1080=14, feather_falloff=0.8),

    "03 whisper (ultra sensitive)": preset(
        "faint ghosts of the tiniest motion — max sensitivity, big soft halo.",
        alpha_floor=0.0, threshold=0.012, mv_gain=80, feather_px_1080=24,
        feather_falloff=0.6, persist_seconds=1.0),

    "04 hard cutout": preset(
        "crisp opaque moving subjects with a hard edge (push-to-full + sharp falloff).",
        alpha_floor=0.0, push_full=1, feather_falloff=2.5, feather_px_1080=3,
        padding_px_1080=6, persist_seconds=0.3),

    "05 halo bloom": preset(
        "soft glowing halos around motion (huge feather, gentle falloff).",
        alpha_floor=0.0, feather_px_1080=60, feather_falloff=0.4, padding_px_1080=10,
        persist_seconds=1.5, mv_gain=40),

    "06 xray (MV heatmap)": preset(
        "DEBUG: visualize the codec's own motion-vector field as a heatmap.",
        debug_mode=1),

    "07 tile inspector": preset(
        "DEBUG: watch which tiles the compositor repaints — the 'less work' engine, live.",
        debug_mode=2, tile_px=48),

    "08 binary mask": preset(
        "DEBUG: stark white-on-black silhouette of what counts as motion.",
        debug_mode=3),

    "09 effect off": preset(
        "plain opaque video — the A/B baseline with no transparency.",
        debug_mode=4),

    "10 pixel-diff (v1 engine)": preset(
        "the old v1 engine: full-frame pixel diff instead of codec vectors. for comparison.",
        engine="diff", alpha_floor=0.0, threshold=0.05, persist_seconds=0.6,
        feather_px_1080=10),

    "11 coarse blocks": preset(
        "chunky, cheap, low-detail mask (big macroblock grid + big tiles).",
        alpha_floor=0.0, mv_block_px=32, tile_px=128, feather_px_1080=20, mv_gain=20),

    "12 fine detail": preset(
        "high-resolution mask that hugs edges (small grid + small tiles).",
        alpha_floor=0.0, mv_block_px=8, tile_px=24, feather_px_1080=4, mv_gain=32),

    "13 twitchy (no persist)": preset(
        "instant, reactive, flickery — no persistence, scene cuts allowed through.",
        alpha_floor=0.0, persist_seconds=0.0, threshold=0.02, mv_gain=50,
        scene_cut_ignore=False),

    "14 sticky smear": preset(
        "everything that moves smears for a full 5 seconds (max persistence).",
        alpha_floor=0.0, persist_seconds=5.0, feather_px_1080=30, feather_falloff=0.7),

    "15 half-visible (high floor)": preset(
        "mostly-visible video; static regions only partly fade (high alpha floor).",
        alpha_floor=0.6, threshold=0.04, feather_px_1080=16, persist_seconds=1.0),

    "16 noise reject (signal only)": preset(
        "only big, coherent motion survives — kills grain/speckle (high threshold + min region).",
        alpha_floor=0.0, threshold=0.08, min_region_px_1080=120, push_full=20,
        persist_seconds=0.6, mv_gain=22),

    "17 hyper MV (pure vectors)": preset(
        "pure motion-vector engine, cranked — the codec's raw motion, unfiltered.",
        engine="mv", alpha_floor=0.0, mv_gain=100, threshold=0.02, feather_px_1080=10),

    "18 strobe (let cuts flash)": preset(
        "scene-cut suppression OFF and twitchy — hard cuts and flashes punch through.",
        alpha_floor=0.0, scene_cut_ignore=False, scene_cut_thresh=0.5,
        threshold=0.03, persist_seconds=0.2),
}


def main() -> None:
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in PRESETS.items():
        path = PRESETS_DIR / f"{name}.json"
        path.write_text(json.dumps(data, indent=2))
        print(f"  wrote {path.name:36s} — {data['_note']}")
    print(f"\n{len(PRESETS)} presets written to {PRESETS_DIR}")


if __name__ == "__main__":
    main()

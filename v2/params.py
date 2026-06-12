"""runtime parameters shared by the worker + control panel.

keeps v1's whole mask-shaping surface (so the look + presets carry over) and adds the
v2 engine knobs (motion-vector vs pixel-diff, block/tile sizes, sync). persisted to its
own settings file so it never clobbers v1's settings.json.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger("travis.v2")

_ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = _ROOT / "settings_v2.json"
PRESETS_DIR = _ROOT / "presets"

_TRANSIENT = {"paused"}
_PRESET_EXCLUDED = _TRANSIENT | {
    "controls_x", "controls_y", "controls_w", "controls_h",
    "player_x", "player_y", "player_w", "player_h",
    "volume",
}

# engine modes
ENGINE_MV = "mv"        # codec motion vectors (the point of v2)
ENGINE_DIFF = "diff"    # v1-style pixel diff (fallback / comparison)
ENGINE_AUTO = "auto"    # MVs when present, pixel-diff on I-frames / MV-less frames
ENGINES = (ENGINE_MV, ENGINE_DIFF, ENGINE_AUTO)

# debug views
VIEW_NORMAL = 0
VIEW_MV = 1         # motion-vector / activity heatmap
VIEW_TILES = 2      # dirty-tile map
VIEW_BINARY = 3     # binary alpha mask
VIEW_OFF = 4        # effect off, opaque video


def auto_params(info: dict) -> dict:
    """Recommend engine values from the current stream's codec + resolution.

    block size tracks the codec's coding-block granularity (h264 macroblocks are 16px; HEVC/
    VP9/AV1 use larger coding units, so a coarser grid matches and stays cheap), and is
    coarsened further for >=1440p. tile size targets ~28 tiles across, stepped to 16. the
    diff-fallback proc divisor scales with height.
    """
    codec = (info.get("codec") or "").lower()
    w = int(info.get("width") or 1920)
    h = int(info.get("height") or 1080)

    if any(c in codec for c in ("hevc", "h265", "vp9", "av1")):
        block = 32
    else:                                   # h264, mpeg2/4, vc1, ...
        block = 16
    if h >= 1440:                           # 4K-ish: coarsen the grid to keep it light
        block = max(block, 32)

    tile = max(32, min(128, int(round((w / 28) / 16)) * 16))   # ~28 across, stepped to 16
    proc = 4 if h <= 540 else 8 if h <= 1440 else 16

    return {"mv_block_px": block, "tile_px": tile, "proc_div": proc}


def safe_preset_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip())
    return name[:64].strip()


def list_presets() -> list[str]:
    if not PRESETS_DIR.exists():
        return []
    return sorted(p.stem for p in PRESETS_DIR.glob("*.json"))


@dataclass
class Params:
    # --- engine ---
    engine: str = ENGINE_AUTO         # mv | diff | auto
    mv_block_px: int = 16             # macroblock grid size the activity field is built at
    mv_gain: float = 28.0            # scales MV pixel-magnitude → activity (higher = more sensitive)
    tile_px: int = 64                # compositor tile size for dirty-region tracking
    iframe_hold: bool = True         # on MV-less frames, hold the previous mask instead of blanking

    # --- visibility ---
    alpha_floor: float = 0.05         # min alpha for fully-static regions
    threshold: float = 0.04           # activity floor (0..1) below which a block is treated as static

    # --- region shaping (pixels @ 1080p reference, auto-scaled) ---
    padding_px_1080: int = 0
    feather_px_1080: int = 8
    feather_falloff: float = 1.0
    min_region_px_1080: int = 0
    push_full: int = 0

    # --- temporal ---
    persist_seconds: float = 0.5
    scene_cut_ignore: bool = True
    scene_cut_thresh: float = 0.7

    # --- compute ---
    proc_div: int = 8                 # pixel-diff fallback resolution divisor
    # GPU compositing (CuPy). off by default: profiling showed it's transfer-bound at 1080p
    # (the premul was never the bottleneck) so it's ~break-even. worth flipping on for 4K+.
    use_gpu: bool = False

    # --- view ---
    debug_mode: int = VIEW_NORMAL
    paused: bool = False              # transient

    # --- audio / sync ---
    volume: int = 100
    resync_interval_s: float = 5.0    # how often the clock checks A/V alignment
    resync_threshold_ms: float = 120  # drift beyond this triggers a catch-up

    # --- window geometry (auto-saved; -1 = default) ---
    controls_x: int = -1
    controls_y: int = -1
    controls_w: int = 460
    controls_h: int = 760
    player_x: int = -1
    player_y: int = -1
    player_w: int = 960
    player_h: int = 540

    # ── persistence ──
    def save(self, path: Path = SETTINGS_FILE) -> None:
        try:
            data = {k: v for k, v in asdict(self).items() if k not in _TRANSIENT}
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            log.exception("failed to save settings")

    def load(self, path: Path = SETTINGS_FILE) -> None:
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            valid = {f.name for f in fields(self)}
            for k, v in data.items():
                if k in valid and k not in _TRANSIENT:
                    setattr(self, k, v)
            log.info("loaded settings from %s", path)
        except Exception:
            log.exception("failed to load settings — using defaults")

    def save_preset(self, name: str) -> Path | None:
        safe = safe_preset_name(name)
        if not safe:
            return None
        PRESETS_DIR.mkdir(exist_ok=True)
        path = PRESETS_DIR / f"{safe}.json"
        try:
            data = {k: v for k, v in asdict(self).items() if k not in _PRESET_EXCLUDED}
            path.write_text(json.dumps(data, indent=2))
            log.info("saved preset '%s'", safe)
            return path
        except Exception:
            log.exception("failed to save preset '%s'", safe)
            return None

    def load_preset(self, name: str) -> bool:
        safe = safe_preset_name(name)
        path = PRESETS_DIR / f"{safe}.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text())
            valid = {f.name for f in fields(self)}
            for k, v in data.items():
                if k in valid and k not in _PRESET_EXCLUDED:
                    setattr(self, k, v)
            return True
        except Exception:
            log.exception("failed to load preset '%s'", safe)
            return False

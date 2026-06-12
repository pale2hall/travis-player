"""v2 entrypoint:  python -m v2 <video|url>"""

from __future__ import annotations

import atexit
import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from . import audio
from .controls import ControlPanel
from .params import Params
from .player import PlayerWindow

log = logging.getLogger("travis.v2")


def _setup_logging() -> None:
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(exist_ok=True)
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
                            datefmt="%H:%M:%S")
    fh = RotatingFileHandler(log_dir / "travis_v2.log", maxBytes=2_000_000, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    def _hook(t, e, tb):
        log.critical("UNCAUGHT:\n%s", "".join(traceback.format_exception(t, e, tb)))
    sys.excepthook = _hook


def main() -> int:
    # source is optional — launch empty and drag a video/URL in, or pass one
    source: str | None = sys.argv[1] if len(sys.argv) > 1 else None
    if source and "://" not in source and not Path(source).exists():
        print(f"not found: {source}", file=sys.stderr)
        return 2

    _setup_logging()
    log.info("=" * 60)
    log.info("travis-player v2 starting  source=%s", source or "(idle — waiting for a drop)")

    audio.set_job_handle(audio.create_job_for_children())

    app = QApplication(sys.argv)
    params = Params()
    params.load()

    player = PlayerWindow(source, params)
    controls = ControlPanel(params, player)

    def _cleanup():
        try:
            player.cleanup()
        except Exception:
            pass
    app.aboutToQuit.connect(_cleanup)
    atexit.register(_cleanup)

    player.show()
    controls.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

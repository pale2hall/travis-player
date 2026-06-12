"""control panel — live tuning. ported from v1 with a new Engine group + v2 debug views.

uses a small declarative slider helper so the panel stays compact. every change writes the
shared Params (read live by the worker) and persists to settings_v2.json.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QSlider, QVBoxLayout, QWidget,
)

from . import params as P
from .params import Params, auto_params, list_presets, safe_preset_name
from .player import PlayerWindow


class ControlPanel(QWidget):
    def __init__(self, params: Params, player: PlayerWindow) -> None:
        super().__init__()
        self.params = params
        self.player = player
        self.setWindowTitle("travis v2 controls")
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)
        self.setAcceptDrops(True)   # drop files / stream URLs here too, not just on the player
        self._labels: list = []   # (QLabel, fn) for refresh
        self._stream_info: dict = {}

        self.resize(max(300, params.controls_w), max(400, params.controls_h))
        if params.controls_x >= 0 and params.controls_y >= 0:
            self.move(params.controls_x, params.controls_y)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        self.v = QVBoxLayout(body)
        self.v.setSpacing(6)

        self._build_presets()
        self._build_view()
        self._build_engine()
        self._build_sliders()
        self._build_playback()
        self._build_buttons()
        self.v.addStretch()

        player.stats_changed.connect(self._on_stats)
        player.position_changed.connect(self._on_position)
        player.stream_info_changed.connect(self._on_stream_info)
        self._refresh()

    # ── helpers ──
    def _group(self, title: str) -> QVBoxLayout:
        gb = QGroupBox(title)
        lay = QVBoxLayout(gb)
        self.v.addWidget(gb)
        return lay

    def _add_slider(self, lay, label_fn, lo, hi, value, on_change, tip="", auto_field=None):
        lbl = QLabel()
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        s.setValue(value)
        if tip:
            s.setToolTip(tip)

        def handler(v):
            on_change(v)
            self._refresh()
            self.params.save()
        s.valueChanged.connect(handler)

        if auto_field:
            # slider + an "Auto" button that derives the value from the current stream
            row = QHBoxLayout()
            row.addWidget(s, stretch=1)
            btn = QPushButton("Auto")
            btn.setMaximumWidth(52)
            btn.setToolTip("Set from the current stream's codec / resolution")
            btn.clicked.connect(lambda _=False, f=auto_field, sl=s: self._apply_auto(f, sl))
            row.addWidget(btn)
            lay.addLayout(row)
        else:
            lay.addWidget(s)
        self._labels.append((lbl, label_fn))
        return s

    def _apply_auto(self, field: str, slider: QSlider) -> None:
        rec = auto_params(self._stream_info)
        if field in rec:
            slider.setValue(int(rec[field]))   # fires the handler → updates param + saves
            self.player.osd(f"auto: {field} = {rec[field]}")

    def _on_stream_info(self, info: dict) -> None:
        self._stream_info = info
        if hasattr(self, "lbl_stream"):
            self.lbl_stream.setText(
                f"stream: {info.get('codec','?')}  "
                f"{info.get('width','?')}×{info.get('height','?')}  "
                f"{info.get('fps',0):.2f}fps")

    def _refresh(self) -> None:
        for lbl, fn in self._labels:
            lbl.setText(fn())

    # ── groups ──
    def _build_presets(self) -> None:
        lay = self._group("Presets")
        row = QHBoxLayout()
        self.preset_dd = QComboBox()
        self.preset_dd.addItems(list_presets())
        row.addWidget(self.preset_dd, stretch=2)
        btn = QPushButton("Load")
        btn.clicked.connect(self._load_preset)
        row.addWidget(btn)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        self.preset_name = QLineEdit()
        self.preset_name.setPlaceholderText("save current as…")
        self.preset_name.returnPressed.connect(self._save_preset)
        row2.addWidget(self.preset_name, stretch=2)
        btn2 = QPushButton("Save")
        btn2.clicked.connect(self._save_preset)
        row2.addWidget(btn2)
        lay.addLayout(row2)

    def _build_view(self) -> None:
        lay = self._group("View")
        self.cb_mode = QComboBox()
        self.cb_mode.addItems([
            "Normal (transparent)", "MV / activity heatmap", "Dirty-tile map",
            "Binary mask", "Effect off (opaque)",
        ])
        self.cb_mode.setCurrentIndex(self.params.debug_mode)
        self.cb_mode.currentIndexChanged.connect(self._on_mode)
        lay.addWidget(self.cb_mode)

    def _build_engine(self) -> None:
        lay = self._group("Engine (codec-driven)")
        self.lbl_stream = QLabel("stream: —")
        self.lbl_stream.setStyleSheet("color:#88a;")
        self.lbl_stream.setToolTip("Codec / resolution the 'Auto' buttons derive values from.")
        lay.addWidget(self.lbl_stream)
        self.cb_engine = QComboBox()
        self.cb_engine.addItems(["Motion vectors", "Pixel diff (v1)", "Auto (MV + diff fallback)"])
        self.cb_engine.setCurrentIndex({P.ENGINE_MV: 0, P.ENGINE_DIFF: 1, P.ENGINE_AUTO: 2}
                                       .get(self.params.engine, 2))
        self.cb_engine.setToolTip("MV = read the codec's own motion vectors. Diff = v1 full-frame "
                                  "pixel diff. Auto = MVs when present, diff on I-frames.")
        self.cb_engine.currentIndexChanged.connect(self._on_engine)
        lay.addWidget(self.cb_engine)

        self._add_slider(
            lay, lambda: f"MV sensitivity (gain):  {self.params.mv_gain:.0f}",
            4, 100, int(self.params.mv_gain), self._set_mv_gain,
            "Scales motion-vector magnitude into activity. Higher = subtler motion stays visible.")
        self._add_slider(
            lay, lambda: f"Macroblock grid:  {self.params.mv_block_px}px",
            8, 32, self.params.mv_block_px, self._set_block,
            "Resolution the activity field is built at. 16 matches h264 macroblocks. "
            "Auto picks from the stream codec (16 for h264, 32 for HEVC/AV1/4K).",
            auto_field="mv_block_px")
        self._add_slider(
            lay, lambda: f"Compositor tile:  {self.params.tile_px}px",
            16, 128, self.params.tile_px, self._set_tile,
            "Dirty-region granularity. Smaller = finer change tracking, more tiles to check. "
            "Auto targets ~28 tiles across the frame.",
            auto_field="tile_px")
        self.cb_hold = QCheckBox("Hold mask on I-frames (no MVs)")
        self.cb_hold.setChecked(self.params.iframe_hold)
        self.cb_hold.toggled.connect(self._on_hold)
        lay.addWidget(self.cb_hold)

        from . import gpu
        if gpu.available():
            self.cb_gpu = QCheckBox(f"GPU compositing — {gpu.device_name()}")
            self.cb_gpu.setChecked(self.params.use_gpu)
            self.cb_gpu.setToolTip("Run mask upscale + premultiply on the GPU (CuPy). "
                                   "At 1080p this is ~break-even (transfer-bound — the math was "
                                   "never the bottleneck). Likely a win at 4K+.")
        else:
            self.cb_gpu = QCheckBox("GPU compositing — unavailable (CuPy/CUDA not found)")
            self.cb_gpu.setChecked(False)
            self.cb_gpu.setEnabled(False)
        self.cb_gpu.toggled.connect(self._on_gpu)
        lay.addWidget(self.cb_gpu)

    def _build_sliders(self) -> None:
        p = self.params
        lay = self._group("Visibility")
        self._add_slider(lay, lambda: f"Static opacity (alpha floor):  {p.alpha_floor:.2f}",
                         0, 100, int(p.alpha_floor * 100), self._set_alpha,
                         "How visible static regions are. 0 = fully transparent.")

        lay = self._group("Motion")
        self._add_slider(lay, lambda: f"Activity threshold:  {p.threshold:.3f}",
                         1, 200, int(p.threshold * 1000), self._set_thresh,
                         "Activity floor below which a block counts as static.")
        self._add_slider(lay, lambda: f"Min region:  {p.min_region_px_1080}px @1080p",
                         0, 400, p.min_region_px_1080, self._set_minr,
                         "Drop motion blobs smaller than this. 0 = off.")

        lay = self._group("Region shape")
        self._add_slider(lay, lambda: f"Padding:  {p.padding_px_1080}px @1080p",
                         0, 60, p.padding_px_1080, self._set_pad)
        self._add_slider(lay, lambda: f"Feather:  {p.feather_px_1080}px @1080p",
                         1, 80, p.feather_px_1080, self._set_feather)
        self._add_slider(lay, lambda: f"Falloff curve:  {p.feather_falloff:.2f}",
                         20, 300, int(p.feather_falloff * 100), self._set_falloff)
        self._add_slider(lay, lambda: ("Push to full:  off" if p.push_full == 0
                                       else f"Push to full:  >{p.push_full}% → 100%"),
                         0, 100, p.push_full, self._set_pushfull)

        lay = self._group("Temporal")
        self._add_slider(lay, lambda: (f"Persistence:  {p.persist_seconds:.1f}s"
                                       if p.persist_seconds > 0 else "Persistence:  off"),
                         0, 50, int(p.persist_seconds * 10), self._set_persist,
                         "Recently-active regions linger this long. Higher = motion trails.")
        self.cb_scene = QCheckBox("Ignore scene cuts")
        self.cb_scene.setChecked(p.scene_cut_ignore)
        self.cb_scene.toggled.connect(self._on_scene)
        lay.addWidget(self.cb_scene)

        self.lbl_status = QLabel("—")
        self.lbl_status.setStyleSheet("color:#8c8; font-style:italic;")
        lay.addWidget(self.lbl_status)

    def _build_playback(self) -> None:
        lay = self._group("Playback")
        self.lbl_pos = QLabel("0:00 / 0:00")
        lay.addWidget(self.lbl_pos)
        self.sl_seek = QSlider(Qt.Orientation.Horizontal)
        self.sl_seek.setRange(0, 1000)
        self.sl_seek.sliderReleased.connect(
            lambda: self.player.seek_to(self.sl_seek.value() / 1000.0))
        self.sl_seek.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.sl_seek.sliderReleased.connect(lambda: setattr(self, "_seeking", False))
        self._seeking = False
        lay.addWidget(self.sl_seek)
        row = QHBoxLayout()
        row.addWidget(QLabel("Volume"))
        self.sl_vol = QSlider(Qt.Orientation.Horizontal)
        self.sl_vol.setRange(0, 100)
        self.sl_vol.setValue(self.params.volume)
        self.sl_vol.valueChanged.connect(self.player.set_volume)
        row.addWidget(self.sl_vol)
        lay.addLayout(row)

    def _build_buttons(self) -> None:
        row = QHBoxLayout()
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self._toggle_pause)
        row.addWidget(self.btn_pause)
        b2 = QPushButton("Fullscreen")
        b2.clicked.connect(self.player.toggle_fullscreen)
        row.addWidget(b2)
        b3 = QPushButton("Quit")
        from PyQt6.QtWidgets import QApplication
        b3.clicked.connect(lambda: QApplication.instance().quit())
        row.addWidget(b3)
        self.v.addLayout(row)

    # ── setters (param writes; _refresh + save handled by the slider handler) ──
    def _set_mv_gain(self, v): self.params.mv_gain = float(v)
    def _set_block(self, v): self.params.mv_block_px = max(8, v)
    def _set_tile(self, v): self.params.tile_px = max(16, v)
    def _set_alpha(self, v): self.params.alpha_floor = v / 100.0
    def _set_thresh(self, v): self.params.threshold = v / 1000.0
    def _set_minr(self, v): self.params.min_region_px_1080 = v
    def _set_pad(self, v): self.params.padding_px_1080 = v
    def _set_feather(self, v): self.params.feather_px_1080 = max(1, v)
    def _set_falloff(self, v): self.params.feather_falloff = v / 100.0
    def _set_pushfull(self, v): self.params.push_full = v
    def _set_persist(self, v): self.params.persist_seconds = v / 10.0

    def _on_mode(self, idx): self.params.debug_mode = idx; self.params.save()
    def _on_engine(self, idx): self.params.engine = P.ENGINES[idx]; self.params.save()
    def _on_hold(self, on): self.params.iframe_hold = on; self.params.save()
    def _on_gpu(self, on): self.params.use_gpu = on; self.params.save()
    def _on_scene(self, on): self.params.scene_cut_ignore = on; self.params.save()

    def _on_stats(self, text: str) -> None:
        self.lbl_status.setText(text)

    def _on_position(self, pos_ms: float, dur_ms: float) -> None:
        if getattr(self, "_seeking", False):
            return
        def _fmt(ms):
            s = int(ms / 1000)
            return f"{s // 60}:{s % 60:02d}"
        self.lbl_pos.setText(f"{_fmt(pos_ms)} / {_fmt(dur_ms)}")
        if dur_ms > 0:
            self.sl_seek.blockSignals(True)
            self.sl_seek.setValue(int(1000 * min(1.0, pos_ms / dur_ms)))
            self.sl_seek.blockSignals(False)

    def _toggle_pause(self) -> None:
        new = not self.params.paused
        self.player.set_paused(new)
        self.btn_pause.setText("Resume" if new else "Pause")

    # ── presets ──
    def _save_preset(self) -> None:
        name = self.preset_name.text().strip()
        if not name:
            return
        if self.params.save_preset(name):
            self.preset_name.clear()
            self.preset_dd.clear()
            self.preset_dd.addItems(list_presets())
            self.preset_dd.setCurrentText(safe_preset_name(name))
            self.player.osd(f"saved preset: {name}")

    def _load_preset(self) -> None:
        name = self.preset_dd.currentText().strip()
        if name and self.params.load_preset(name):
            self._sync_from_params()
            self.params.save()
            self.player.osd(f"loaded preset: {name}")

    def _sync_from_params(self) -> None:
        """Push params back into widgets after a preset load, without re-firing handlers."""
        p = self.params
        pairs = [
            (self.cb_mode, "setCurrentIndex", p.debug_mode),
            (self.cb_engine, "setCurrentIndex",
             {P.ENGINE_MV: 0, P.ENGINE_DIFF: 1, P.ENGINE_AUTO: 2}.get(p.engine, 2)),
            (self.cb_hold, "setChecked", p.iframe_hold),
            (self.cb_scene, "setChecked", p.scene_cut_ignore),
        ]
        for w, setter, val in pairs:
            w.blockSignals(True)
            getattr(w, setter)(val)
            w.blockSignals(False)
        self._refresh()

    def _save_geometry(self) -> None:
        self.params.controls_x, self.params.controls_y = self.x(), self.y()
        self.params.controls_w, self.params.controls_h = self.width(), self.height()
        self.params.save()

    def resizeEvent(self, e):
        super().resizeEvent(e); self._save_geometry()

    def moveEvent(self, e):
        super().moveEvent(e); self._save_geometry()

    # ── drag-and-drop (files or stream URLs) → forward to the player ──
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        md = e.mimeData()
        source = None
        if md.hasUrls():
            url = md.urls()[0]
            source = url.toLocalFile() if url.isLocalFile() else url.toString()
        elif md.hasText():
            source = md.text().strip()
        if source:
            self.player.load_source(source)
            self.player.osd(f"loading: {source[:60]}", 2.5)

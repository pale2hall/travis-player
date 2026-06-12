"""transparent player window — persistent buffer, dirty-rect repaint.

unlike v1 (which built and copied a fresh full-frame QImage every frame), v2 wraps the
compositor's persistent RGBA buffer in a single QImage built once, and on each frame repaints
only the dirty rects the worker reports. fully-static scenes repaint nothing.

window chrome, drag/resize, the seek+volume overlay, fullscreen, keys and drag-drop are
ported from v1.
"""

from __future__ import annotations

import logging
import os
import time

from PyQt6.QtCore import QPoint, QRect, Qt, QThread, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QDragEnterEvent, QDropEvent, QImage, QKeyEvent, QMouseEvent,
    QPainter, QPolygon, QRegion,
)
from PyQt6.QtWidgets import QApplication, QWidget

from .audio import AudioController
from .clock import MasterClock
from .compose import Compositor
from .params import Params
from .worker import FrameWorker

log = logging.getLogger("travis.v2")


class PlayerWindow(QWidget):
    position_changed = pyqtSignal(float, float)
    stats_changed = pyqtSignal(str)
    stream_info_changed = pyqtSignal(dict)

    def __init__(self, source: str, params: Params) -> None:
        super().__init__()
        self.params = params
        self._source = source
        self.setWindowTitle("travis-player v2")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)

        self.resize(max(200, params.player_w), max(150, params.player_h))
        if params.player_x >= 0 and params.player_y >= 0:
            self.move(params.player_x, params.player_y)

        self._image: QImage | None = None
        self._img_generation = -1
        self._stream_info: dict = {}
        self._drag_pos: QPoint | None = None
        self._osd_text = ""
        self._osd_until = 0.0
        self._hovered = False
        self._chrome_h = 28
        self._grip_size = 22
        self._BAR_H = 40
        self._pos_ms = 0.0
        self._dur_ms = 0.0
        self._seek_dragging = False
        self._vol_dragging = False

        # shared state across the worker/GUI boundary
        self._clock = MasterClock()
        self._compositor = Compositor()
        self._start_worker(source)

        self._audio = AudioController(source)
        self._audio.start()
        if params.volume != 100:
            self._audio.set_volume(params.volume)

    # ── worker lifecycle ──
    def _start_worker(self, source: str) -> None:
        self._thread = QThread(self)
        self._worker = FrameWorker(source, self.params, self._clock, self._compositor)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.position_update.connect(self._on_position)
        self._worker.stats_update.connect(self.stats_changed)
        self._worker.stream_info.connect(self._on_stream_info)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

    def _on_stream_info(self, info: dict) -> None:
        self._stream_info = info
        self.stream_info_changed.emit(info)

    # ── frame handling ──
    def _ensure_image(self) -> None:
        c = self._compositor
        if c.buffer is None:
            self._image = None
            return
        if self._image is None or self._img_generation != c.generation:
            self._image = QImage(
                c.buffer.data, c.width, c.height, c.width * 4,
                QImage.Format.Format_RGBA8888_Premultiplied,
            )
            self._img_generation = c.generation

    def _video_to_widget(self) -> tuple[float, float, float]:
        """scale + (ox, oy) offset mapping video pixels → widget pixels (letterboxed)."""
        c = self._compositor
        if c.width == 0 or c.height == 0:
            return 1.0, 0.0, 0.0
        scale = min(self.width() / c.width, self.height() / c.height)
        dw, dh = c.width * scale, c.height * scale
        return scale, (self.width() - dw) / 2, (self.height() - dh) / 2

    def _on_frame(self, rects: list) -> None:
        self._ensure_image()
        if self._image is None:
            return
        if len(rects) > 40:
            self.update()
            return
        scale, ox, oy = self._video_to_widget()
        region = QRegion()
        for (x, y, w, h) in rects:
            region += QRect(int(ox + x * scale) - 1, int(oy + y * scale) - 1,
                            int(w * scale) + 3, int(h * scale) + 3)
        self.update(region)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)

        self._ensure_image()
        if self._image is not None:
            scale, ox, oy = self._video_to_widget()
            dest = QRect(int(ox), int(oy),
                         int(self._compositor.width * scale),
                         int(self._compositor.height * scale))
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            with self._compositor.lock:
                p.drawImage(dest, self._image)

        self._paint_chrome(p)

    def _paint_chrome(self, p: QPainter) -> None:
        if self._hovered and not self.isFullScreen():
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(20, 20, 20, 170)))
            p.drawRect(0, 0, self.width(), self._chrome_h)
            p.setPen(QColor(255, 255, 255, 200))
            p.drawText(10, self._chrome_h - 8, "⋮⋮  drag to move    [F11] fullscreen    [Q] quit")
            g = self._grip_size
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(255, 255, 255, 180)))
            p.drawPolygon(QPolygon([
                QPoint(self.width(), self.height() - g),
                QPoint(self.width(), self.height()),
                QPoint(self.width() - g, self.height()),
            ]))

        if time.monotonic() < self._osd_until and self._osd_text:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            y_top = self._chrome_h if self._hovered else 0
            p.fillRect(0, y_top, self.width(), 30, Qt.GlobalColor.black)
            p.setPen(Qt.GlobalColor.white)
            p.drawText(10, y_top + 22, self._osd_text)

        if self._hovered and not self.isFullScreen():
            self._paint_bar(p)

    # ── bottom seek + volume overlay (ported from v1) ──
    _VOL_W, _TIME_W, _PAD, _ICON_W = 72, 48, 4, 18

    def _seek_rect(self) -> QRect:
        W, H = self.width(), self.height()
        bar_y = H - self._BAR_H
        left = self._PAD + self._TIME_W + self._PAD
        right = W - (self._PAD + self._TIME_W + self._PAD + self._ICON_W + self._PAD + self._VOL_W + self._PAD)
        return QRect(left, bar_y + (self._BAR_H - 6) // 2, max(10, right - left), 6)

    def _vol_rect(self) -> QRect:
        W, H = self.width(), self.height()
        return QRect(W - self._PAD - self._VOL_W, (H - self._BAR_H) + (self._BAR_H - 6) // 2, self._VOL_W, 6)

    def _paint_bar(self, p: QPainter) -> None:
        W, H = self.width(), self.height()
        bar_y = H - self._BAR_H
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(20, 20, 20, 200)))
        p.drawRect(0, bar_y, W, self._BAR_H)
        text_y = bar_y + self._BAR_H // 2 + 5

        def _fmt(ms: float) -> str:
            s = int(ms / 1000)
            return f"{s // 60}:{s % 60:02d}"

        p.setPen(QColor(200, 200, 200))
        p.drawText(self._PAD, text_y, _fmt(self._pos_ms))
        sr = self._seek_rect()
        p.drawText(sr.right() + self._PAD, text_y, _fmt(self._dur_ms))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(80, 80, 80)))
        p.drawRoundedRect(sr, 3, 3)
        if self._dur_ms > 0:
            frac = min(1.0, self._pos_ms / self._dur_ms)
            fw = int(sr.width() * frac)
            if fw > 0:
                p.setBrush(QBrush(QColor(255, 255, 255, 220)))
                p.drawRoundedRect(QRect(sr.x(), sr.y(), fw, sr.height()), 3, 3)
            p.setBrush(QBrush(QColor(255, 255, 255)))
            p.drawEllipse(sr.x() + fw - 5, sr.y() - 3, 10, 12)

        icon_x = sr.right() + self._PAD + self._TIME_W + self._PAD
        p.setPen(QColor(200, 200, 200))
        p.drawText(icon_x, text_y, "🔊" if self.params.volume > 0 else "🔇")
        vr = self._vol_rect()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(80, 80, 80)))
        p.drawRoundedRect(vr, 3, 3)
        vw = int(vr.width() * (self.params.volume / 100.0))
        if vw > 0:
            p.setBrush(QBrush(QColor(100, 200, 100, 220)))
            p.drawRoundedRect(QRect(vr.x(), vr.y(), vw, vr.height()), 3, 3)
        p.setBrush(QBrush(QColor(200, 255, 200)))
        p.drawEllipse(vr.x() + vw - 5, vr.y() - 3, 10, 12)

    # ── osd + position ──
    def osd(self, text: str, seconds: float = 1.5) -> None:
        self._osd_text = text
        self._osd_until = time.monotonic() + seconds
        self.update()

    def _on_position(self, pos_ms: float, dur_ms: float) -> None:
        self._pos_ms, self._dur_ms = pos_ms, dur_ms
        self.position_changed.emit(pos_ms, dur_ms)
        if self._hovered:
            self.update(QRect(0, self.height() - self._BAR_H, self.width(), self._BAR_H))

    # ── audio + seek ──
    def set_volume(self, vol: int) -> None:
        vol = max(0, min(100, vol))
        self.params.volume = vol
        if self._audio is not None:
            self._audio.set_volume(vol)
        self.params.save()
        self.update()

    def seek_to(self, frac: float) -> None:
        self._worker.request_seek(frac)
        if self._dur_ms > 0 and self._audio is not None:
            self._audio.seek(frac * self._dur_ms / 1000.0)

    def set_paused(self, paused: bool) -> None:
        self.params.paused = paused
        self._clock.set_paused(paused)
        if self._audio is not None:
            self._audio.set_paused(paused)
        self.osd("paused" if paused else "playing")

    # ── mouse: drag / resize / bar ──
    _RESIZE_MARGIN = 12

    def _in_bar(self, pos: QPoint) -> bool:
        return pos.y() >= self.height() - self._BAR_H

    def _edge_at(self, pos: QPoint):
        m, g = self._RESIZE_MARGIN, self._grip_size
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        if x > w - g and y > h - g:
            return Qt.Edge.RightEdge | Qt.Edge.BottomEdge
        val = 0
        if x < m: val |= int(Qt.Edge.LeftEdge.value)
        if x > w - m: val |= int(Qt.Edge.RightEdge.value)
        if y < m: val |= int(Qt.Edge.TopEdge.value)
        if (not self._hovered) and y > h - m: val |= int(Qt.Edge.BottomEdge.value)
        return Qt.Edge(val) if val else None

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() != Qt.MouseButton.LeftButton or self.isFullScreen():
            return
        pos = e.position().toPoint()
        if self._hovered and self._in_bar(pos):
            sr, vr = self._seek_rect(), self._vol_rect()
            if sr.x() <= pos.x() <= sr.right() + self._TIME_W:
                self._seek_dragging = True
                self._apply_seek(pos.x())
            elif vr.x() - self._ICON_W - self._PAD <= pos.x() <= vr.right():
                self._vol_dragging = True
                self._apply_vol(pos.x())
            return
        edge = self._edge_at(pos)
        wh = self.windowHandle()
        if edge is not None and wh is not None:
            wh.startSystemResize(edge)
            return
        self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _apply_seek(self, mx: int) -> None:
        sr = self._seek_rect()
        self.seek_to(max(0.0, min(1.0, (mx - sr.x()) / max(1, sr.width()))))

    def _apply_vol(self, mx: int) -> None:
        vr = self._vol_rect()
        self.set_volume(int(max(0.0, min(1.0, (mx - vr.x()) / max(1, vr.width()))) * 100))

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        pos = e.position().toPoint()
        if e.buttons() & Qt.MouseButton.LeftButton:
            if self._seek_dragging:
                self._apply_seek(pos.x()); return
            if self._vol_dragging:
                self._apply_vol(pos.x()); return
            if self._drag_pos is not None:
                self.move(e.globalPosition().toPoint() - self._drag_pos); return
        if not self.isFullScreen():
            if self._hovered and self._in_bar(pos):
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                edge = self._edge_at(pos)
                self.setCursor(Qt.CursorShape.SizeFDiagCursor if edge else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, _e: QMouseEvent) -> None:
        self._drag_pos = None
        self._seek_dragging = self._vol_dragging = False

    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.toggle_fullscreen()

    def enterEvent(self, _e) -> None:
        self._hovered = True
        self.update()

    def leaveEvent(self, _e) -> None:
        self._hovered = False
        self.unsetCursor()
        self.update()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        k = e.key()
        if k in (Qt.Key.Key_Q, Qt.Key.Key_Escape):
            QApplication.instance().quit()
        elif k == Qt.Key.Key_Space:
            self.set_paused(not self.params.paused)
        elif k == Qt.Key.Key_F11 or k == Qt.Key.Key_F:
            self.toggle_fullscreen()
        elif k == Qt.Key.Key_R:
            self.reset_geometry()

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal(); self.osd("windowed")
        else:
            self.showFullScreen(); self.osd("fullscreen — F11 to exit")

    def reset_geometry(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        self.resize(960, 540)
        screen = self.screen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.x() + (geo.width() - self.width()) // 2,
                      geo.y() + (geo.height() - self.height()) // 2)
        self.osd("geometry reset")
        self._save_geometry()

    def _save_geometry(self) -> None:
        if self.isFullScreen():
            return
        self.params.player_x, self.params.player_y = self.x(), self.y()
        self.params.player_w, self.params.player_h = self.width(), self.height()
        self.params.save()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._save_geometry()
        self.update()

    def moveEvent(self, e) -> None:
        super().moveEvent(e)
        self._save_geometry()

    # ── drag-and-drop source swap ──
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()
            self.osd("drop to load")

    def dropEvent(self, e: QDropEvent) -> None:
        md = e.mimeData()
        source = None
        if md.hasUrls():
            url = md.urls()[0]
            source = url.toLocalFile() if url.isLocalFile() else url.toString()
        elif md.hasText():
            source = md.text().strip()
        if source:
            self.load_source(source)
            self.osd(f"loading: {source[:60]}", 2.5)

    def load_source(self, source: str) -> None:
        # guard against re-entrant swaps (rapid drops) overlapping their teardowns
        if getattr(self, "_swapping", False):
            log.info("source swap already in progress, ignoring: %s", source)
            return
        self._swapping = True
        try:
            # 1. SILENCE the old audio immediately, then fully kill it BEFORE we start
            #    anything new — otherwise it keeps playing through the worker teardown and
            #    can overlap with the next stream's audio.
            old_audio = self._audio
            self._audio = None
            try:
                old_audio.set_paused(True)   # instant mute
            except Exception:
                log.exception("pausing old audio failed")
            try:
                old_audio.stop()             # terminate (then kill on timeout) — mpv is gone
            except Exception:
                log.exception("audio stop failed")

            # 2. tear down the old worker/decoder
            self._worker.stop()
            self._thread.quit()
            self._thread.wait(2000)

            # 3. bring up the new stream
            self._source = source
            self._image = None
            self._img_generation = -1
            self._start_worker(source)
            self._audio = AudioController(
                source, pipe_name=rf"\\.\pipe\travis-audio-{os.getpid()}-{int(time.monotonic()*1000)}")
            self._audio.start()
            if self.params.volume != 100:
                self._audio.set_volume(self.params.volume)
            if self.params.paused:
                self._audio.set_paused(True)
        finally:
            self._swapping = False

    def cleanup(self) -> None:
        try:
            self.params.save()
        except Exception:
            log.exception("settings save failed")
        self._worker.stop()
        self._thread.quit()
        self._thread.wait(2000)
        try:
            if self._audio is not None:
                self._audio.stop()
        except Exception:
            log.exception("audio stop failed")

    def closeEvent(self, _e) -> None:
        self.cleanup()

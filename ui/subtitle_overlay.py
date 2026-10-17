"""Movable subtitle overlay with multi-monitor support."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget


def _value(obj: Any, names: tuple[str, ...], default: Any = "") -> Any:
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _ensure_fonts() -> None:
    # Qt's offscreen Windows platform may need fonts loaded explicitly.
    if QFontDatabase.families():
        return
    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "segoeuib.ttf"):
        path = font_dir / filename
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))


class SubtitleOverlay(QWidget):
    """Outlined subtitles with one expiry timer shared across updates."""

    def __init__(self, settings: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _ensure_fonts()
        self.settings = settings
        self._english = ""
        self._chinese = ""
        self._drag_offset: QPoint | None = None
        self._user_positioned = False
        self._screen_index = int(getattr(settings, "screen_index", 0))
        self._text_rows: list[tuple[str, QFont, float, QColor]] = []
        self._expire = QTimer(self)
        self._expire.setSingleShot(True)
        self._expire.timeout.connect(self.clear)
        self.setObjectName("subtitleOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumSize(260, 60)
        self._apply_flags()
        self.place_on_screen(self._screen_index)

    def _apply_flags(self) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if bool(getattr(self.settings, "click_through", False)):
            flags |= Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setCursor(
            Qt.CursorShape.ArrowCursor
            if getattr(self.settings, "click_through", False)
            else Qt.CursorShape.SizeAllCursor
        )

    def configure(self, settings: Any) -> None:
        was_visible = self.isVisible()
        old_click_through = bool(self.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        old_screen = self._screen_index
        self.settings = settings
        self._screen_index = int(getattr(settings, "screen_index", 0))
        if old_click_through != bool(getattr(settings, "click_through", False)):
            self._apply_flags()
        self._layout_text()
        if self._screen_index != old_screen:
            self.place_on_screen(self._screen_index)
        if was_visible:
            self.show()
        self.update()

    def set_click_through(self, enabled: bool) -> None:
        setattr(self.settings, "click_through", bool(enabled))
        was_visible = self.isVisible()
        self._apply_flags()
        if was_visible:
            self.show()

    def _screen(self):
        screens = QApplication.screens()
        if not screens:
            return None
        index = max(0, min(self._screen_index, len(screens) - 1))
        return screens[index]

    def place_on_screen(self, index: int) -> None:
        self._screen_index = int(index)
        setattr(self.settings, "screen_index", self._screen_index)
        self._user_positioned = False
        self._layout_text()
        self._place_default()

    def _place_default(self) -> None:
        screen = self._screen()
        if screen is None:
            return
        bounds = screen.availableGeometry()
        bottom_margin = max(36, round(bounds.height() * 0.055))
        self.move(
            bounds.x() + (bounds.width() - self.width()) // 2,
            bounds.bottom() - self.height() - bottom_margin,
        )

    @staticmethod
    def _wrap(text: str, metrics: QFontMetricsF, width: float) -> list[str]:
        """Wrap English at spaces and CJK text at characters."""
        lines: list[str] = []
        for paragraph in text.splitlines() or [""]:
            line = ""
            # Split oversized words at characters only when necessary.
            tokens = re.findall(r"[\u2e80-\u9fff\uf900-\ufaff]|[^\u2e80-\u9fff\uf900-\ufaff\s]+\s*|\s+", paragraph)
            for token in tokens:
                if metrics.horizontalAdvance(line + token) <= width:
                    line += token
                    continue
                if line.strip():
                    lines.append(line.rstrip())
                    line = ""
                token = token.lstrip()
                for char in token:
                    if line and metrics.horizontalAdvance(line + char) > width:
                        lines.append(line.rstrip())
                        line = ""
                    line += char
            if line.strip():
                lines.append(line.rstrip())
        return lines

    def _layout_text(self) -> None:
        screen = self._screen()
        bounds = screen.availableGeometry() if screen else self.geometry()
        width = max(280, min(1600, round(bounds.width() * 0.84)))
        max_height = max(130, int(bounds.height() * 0.48))
        padding = 20.0
        requested = max(12, int(getattr(self.settings, "font_size", 28)))
        color = QColor(str(getattr(self.settings, "color", "#FFFFFF")))
        if not color.isValid():
            color = QColor("#FFFFFF")
        show_english = bool(getattr(self.settings, "show_english", True))
        for size in range(requested, 9, -1):
            rows: list[tuple[str, QFont, float, QColor]] = []
            height = padding
            for text, scale, row_color in (
                (self._english if show_english else "", 0.76, QColor("#DCE6EE")),
                (self._chinese, 1.0, color),
            ):
                if not text:
                    continue
                font = QFont("Microsoft YaHei UI")
                font.setPixelSize(round(size * scale))
                font.setWeight(QFont.Weight.DemiBold)
                metrics = QFontMetricsF(font)
                for line in self._wrap(text, metrics, width - padding * 2):
                    height += metrics.ascent()
                    rows.append((line, font, height, row_color))
                    height += metrics.descent() + max(4.0, size * 0.14)
                height += 4.0
            height += padding - 4.0
            if height <= max_height or size == 10:
                self._text_rows = rows
                self.resize(width, max(70, math.ceil(height)))
                break
        if not self._user_positioned:
            self._place_default()
        elif screen:
            # Keep wrapped subtitles within the screen.
            x = min(max(self.x(), bounds.x()), max(bounds.x(), bounds.right() - self.width() + 1))
            y = min(max(self.y(), bounds.y()), max(bounds.y(), bounds.bottom() - self.height() + 1))
            self.move(x, y)

    def show_subtitle(self, subtitle: Any) -> None:
        if not self._show_text(subtitle):
            return
        seconds = max(1.0, float(getattr(self.settings, "display_seconds", 5.0)))
        self._expire.start(round(seconds * 1000))

    def show_timed_subtitle(self, subtitle: Any) -> None:
        """Let the video timeline clear cues, including while paused."""
        self._expire.stop()
        if subtitle is None:
            self.clear()
            return
        if not self._show_text(subtitle):
            self.clear()

    def _show_text(self, subtitle: Any) -> bool:
        english = str(_value(subtitle, ("english", "source", "text", "source_text", "transcript")))
        chinese = str(_value(subtitle, ("chinese", "translation", "translated", "translated_text", "target_text")))
        if not english.strip() and not chinese.strip():
            return False
        self._english = english.strip()
        self._chinese = chinese.strip()
        self._layout_text()
        self.show()
        self.update()
        return True

    def preview(self) -> None:
        self.show_subtitle(
            {
                "english": "This is a subtitle preview.",
                "chinese": "这是字幕预览。",
            }
        )

    def clear(self) -> None:
        self._expire.stop()
        self._english = ""
        self._chinese = ""
        self._text_rows = []
        self.hide()

    def paintEvent(self, event) -> None:
        if not self._text_rows:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        opacity = min(1.0, max(0.0, float(getattr(self.settings, "background_opacity", 0.48))))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(7, 13, 23, round(opacity * 255)))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 15, 15)
        outline = QColor(str(getattr(self.settings, "outline_color", "#000000")))
        if not outline.isValid():
            outline = QColor("#000000")
        stroke = max(0.0, float(getattr(self.settings, "outline_width", 2)))
        for text, font, baseline, fill in self._text_rows:
            metrics = QFontMetricsF(font)
            x = (self.width() - metrics.horizontalAdvance(text)) / 2
            path = QPainterPath()
            path.addText(x, baseline, font, text)
            if stroke:
                pen = QPen(outline, stroke * 2)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.strokePath(path, pen)
            painter.fillPath(path, fill)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not getattr(self.settings, "click_through", False):
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._user_positioned = True
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
        event.accept()

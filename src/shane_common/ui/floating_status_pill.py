from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

_FLOATING_PILL_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
)


class FloatingStatusPill(QtWidgets.QWidget):
    """
    Generic frameless always-on-top status pill.

    The pill exposes a minimal interaction model that any tool can reuse:
    drag to reposition, double-click to request expansion, and a small context
    menu for an optional primary action plus quit.
    """

    toggle_requested = QtCore.Signal()
    position_changed = QtCore.Signal()
    quit_requested = QtCore.Signal()

    def __init__(
        self,
        parent: Optional[QtWidgets.QWidget] = None,
        *,
        width: int = 260,
        height: int = 40,
        corner_radius: int = 10,
        initial_text: str = "-",
        initial_color: str = "#374151",
        tooltip: str = (
            "Status\n"
            "Double-click to open  |  Drag to reposition  |  Right-click for menu"
        ),
        toggle_action_text: str = "Open",
        quit_action_text: str = "Quit",
        position_x_key: str = "compact_pos_x",
        position_y_key: str = "compact_pos_y",
    ) -> None:
        super().__init__(parent, _FLOATING_PILL_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(width, height)

        self._pill_w = width
        self._pill_h = height
        self._radius = corner_radius
        self._drag_offset: Optional[QtCore.QPoint] = None
        self._bg_color = initial_color
        self._toggle_action_text = toggle_action_text
        self._quit_action_text = quit_action_text
        self._position_x_key = position_x_key
        self._position_y_key = position_y_key

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 10, 0)
        layout.setSpacing(6)

        self._dot = QtWidgets.QLabel("●", self)
        self._dot.setStyleSheet(
            "color: rgba(255,255,255,180); font-size: 9px; background: transparent;"
        )
        self._label = QtWidgets.QLabel(initial_text, self)
        self._label.setStyleSheet(
            "color: #ffffff; font-weight: bold; font-size: 12px;"
            " background: transparent;"
        )

        layout.addWidget(self._dot)
        layout.addWidget(self._label, 1)

        self.setToolTip(tooltip)

    def update_status(self, text: str, hex_color: str) -> None:
        self._bg_color = hex_color
        self._label.setText(text)
        self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:  # type: ignore[override]
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor("#1f2937"), 1))
        painter.setBrush(QtGui.QColor(self._bg_color))
        path = QtGui.QPainterPath()
        path.addRoundedRect(
            QtCore.QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0),
            self._radius,
            self._radius,
        )
        painter.drawPath(path)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # type: ignore[override]
        if (
            event.buttons() & Qt.MouseButton.LeftButton
            and self._drag_offset is not None
        ):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            was_dragging = self._drag_offset is not None
            self._drag_offset = None
            if was_dragging:
                self.position_changed.emit()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(
        self,
        event: QtGui.QMouseEvent,
    ) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            self.toggle_requested.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def contextMenuEvent(
        self,
        event: QtGui.QContextMenuEvent,
    ) -> None:  # type: ignore[override]
        menu = QtWidgets.QMenu(self)
        menu.addAction(self._toggle_action_text, self.toggle_requested.emit)
        menu.addSeparator()
        menu.addAction(self._quit_action_text, self.quit_requested.emit)
        menu.exec(event.globalPos())

    def save_position(self, settings: QtCore.QSettings) -> None:
        settings.setValue(self._position_x_key, self.pos().x())
        settings.setValue(self._position_y_key, self.pos().y())

    def restore_position(self, settings: QtCore.QSettings) -> None:
        try:
            x = int(settings.value(self._position_x_key, -1))
            y = int(settings.value(self._position_y_key, -1))
        except (TypeError, ValueError):
            x, y = -1, -1
        if x >= 0 and y >= 0:
            self.move(x, y)
        else:
            self.move_to_default()

    def move_to_default(self) -> None:
        screen = QtWidgets.QApplication.primaryScreen()
        if screen is None:
            self.move(100, 100)
            return
        geom = screen.availableGeometry()
        self.move(
            geom.right() - self._pill_w - 20,
            geom.bottom() - self._pill_h - 20,
        )
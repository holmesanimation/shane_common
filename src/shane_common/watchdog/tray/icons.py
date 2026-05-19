"""
Generic tray icon utilities.

Provides:
- Color constants for severity tiers.
- make_circle_icon(color_hex, size) — QIcon with a filled circle.
- worst_case_color(liveness_map, color_fn) — derives aggregate tray color
  from a dict mapping app_id → liveness string, using a caller-supplied
  color function.
"""
from __future__ import annotations

from typing import Callable, Mapping, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPixmap

# ---------------------------------------------------------------------------
# Shared color constants
# ---------------------------------------------------------------------------

COLOR_RED    = "#ef4444"
COLOR_ORANGE = "#f97316"
COLOR_YELLOW = "#eab308"
COLOR_GREEN  = "#22c55e"
COLOR_BLUE   = "#60a5fa"
COLOR_GRAY   = "#94a3b8"


# ---------------------------------------------------------------------------
# Icon generation
# ---------------------------------------------------------------------------

def make_circle_icon(color_hex: str, size: int = 32) -> QIcon:
    """
    Return a *size* × *size* QIcon containing a filled circle in *color_hex*.

    Used as the system tray icon.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QBrush(QColor(color_hex)))
    painter.setPen(Qt.PenStyle.NoPen)
    margin = max(1, size // 10)
    painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# Aggregate color derivation
# ---------------------------------------------------------------------------

def worst_case_color(
    liveness_map: Mapping[str, str],
    *,
    urgent_states: frozenset[str],
    warning_states: frozenset[str],
    info_states: frozenset[str],
    healthy_states: frozenset[str],
) -> str:
    """
    Return the hex colour representing the worst-case liveness across all apps.

    Priority order: URGENT (red) > WARNING (orange) > INFO (yellow)
    > HEALTHY (green) > no apps / all unknown (gray).

    Parameters
    ----------
    liveness_map:
        Dict mapping ``app_id`` → liveness string value.
    urgent_states, warning_states, info_states, healthy_states:
        Frozensets of liveness string values for each severity tier.
        Callers supply these so this helper stays agnostic of the liveness enum.
    """
    values = set(liveness_map.values())
    if values & urgent_states:
        return COLOR_RED
    if values & warning_states:
        return COLOR_ORANGE
    if values & info_states:
        return COLOR_YELLOW
    if values & healthy_states:
        return COLOR_GREEN
    return COLOR_GRAY

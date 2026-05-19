"""
LivenessBadge — generic QLabel pill for displaying a liveness state.

Parameterized by a caller-supplied color function so each application can
provide its own liveness-value → hex-color mapping without coupling this
widget to any specific liveness enum.
"""
from __future__ import annotations

from typing import Callable

from PySide6 import QtWidgets


class LivenessBadge(QtWidgets.QLabel):
    """
    A styled QLabel that displays a liveness state as a colored pill.

    Parameters
    ----------
    color_fn:
        Callable that maps a liveness string value to a hex color string.
        The caller is responsible for providing the correct mapping.
    light_bg_states:
        Set of liveness string values that require dark text (because
        the background color is light).
    """

    def __init__(
        self,
        color_fn: Callable[[str], str],
        light_bg_states: frozenset[str] | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._color_fn = color_fn
        self._light_bg_states: frozenset[str] = light_bg_states or frozenset()
        self.setStyleSheet(self._style("UNKNOWN"))
        self.setText("UNKNOWN")

    def set_liveness(self, state: str) -> None:
        """Update the badge to reflect *state*."""
        self.setText(state)
        self.setStyleSheet(self._style(state))

    def _style(self, state: str) -> str:
        bg = self._color_fn(state)
        fg = "#1e293b" if state in self._light_bg_states else "#ffffff"
        return (
            f"QLabel {{ background-color: {bg}; color: {fg}; "
            f"border-radius: 4px; padding: 1px 7px; "
            f"font-weight: bold; font-size: 11px; }}"
        )

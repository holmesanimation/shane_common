# shane_common/src/shane_common/ui/collapsible_section.py
"""
CollapsibleSection — Reusable expand/collapse container widget.

Layout:
    ┌─────────────────────────────────────────────┐
    │ ▼ Section Title                             │  ← header row (always visible)
    ├─────────────────────────────────────────────┤
    │  (child content — hidden when collapsed)    │
    └─────────────────────────────────────────────┘

Usage:
    section = CollapsibleSection("Session Options")
    section.set_content_widget(my_form_widget)
    section.set_expanded(False)      # collapse
    section.set_expanded(True)       # expand
"""
from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtWidgets


class CollapsibleSection(QtWidgets.QWidget):
    """A header row with a toggle button that shows/hides a child content area."""

    toggled = QtCore.Signal(bool)  # True = expanded, False = collapsed

    _ARROW_EXPANDED = "\u25BC"   # ▼
    _ARROW_COLLAPSED = "\u25B6"  # ▶

    def __init__(
        self,
        title: str = "",
        *,
        expanded: bool = True,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._expanded = expanded
        self._content: Optional[QtWidgets.QWidget] = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header row: toggle button (arrow + title)
        self._btn_toggle = QtWidgets.QToolButton()
        self._btn_toggle.setStyleSheet(
            "QToolButton { border: none; font-weight: bold; font-size: 12px; }"
        )
        self._btn_toggle.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._btn_toggle.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._btn_toggle.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self._update_header_text()
        self._btn_toggle.clicked.connect(self._on_toggle)
        root.addWidget(self._btn_toggle)

        # Content area (placeholder until set_content_widget is called)
        self._content_area = QtWidgets.QWidget()
        self._content_layout = QtWidgets.QVBoxLayout(self._content_area)
        self._content_layout.setContentsMargins(0, 4, 0, 0)
        self._content_layout.setSpacing(0)
        root.addWidget(self._content_area)

        # Apply initial state
        self._content_area.setVisible(self._expanded)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_content_widget(self, widget: QtWidgets.QWidget) -> None:
        """Set (or replace) the collapsible content widget."""
        # Remove previous content if any
        if self._content is not None:
            self._content_layout.removeWidget(self._content)
            self._content.setParent(None)

        self._content = widget
        self._content_layout.addWidget(widget)
        widget.setVisible(self._expanded)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        """Programmatically expand or collapse the section."""
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._content_area.setVisible(expanded)
        if self._content is not None:
            self._content.setVisible(expanded)
        self._update_header_text()
        self.toggled.emit(expanded)

    def set_title(self, title: str) -> None:
        self._title = title
        self._update_header_text()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @QtCore.Slot()
    def _on_toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def _update_header_text(self) -> None:
        arrow = self._ARROW_EXPANDED if self._expanded else self._ARROW_COLLAPSED
        self._btn_toggle.setText(f"{arrow}  {self._title}")

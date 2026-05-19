"""
BaseStatusWindow — generic QMainWindow for a watchdog status dashboard.

Behavior:
- closeEvent() hides the window instead of destroying it so the tray
  icon remains active.  This is NOT an application quit event.
- Window geometry is persisted via QSettings(org_name, app_name).
- Provides a refresh_requested Signal and an auto-refresh toggle.
- Subclasses override _build_content(container) to add domain panels.
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import Qt


class BaseStatusWindow(QtWidgets.QMainWindow):
    """
    Base class for a watchdog status dashboard window.

    Parameters
    ----------
    settings_org:
        QSettings organization name (e.g. ``"trading_platform"``).
    settings_app:
        QSettings application name (e.g. ``"SupervisorMonitor"``).
    title:
        Window title string.
    """

    refresh_requested = QtCore.Signal()

    def __init__(
        self,
        settings_org: str,
        settings_app: str,
        title: str = "Status Monitor",
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_org = settings_org
        self._settings_app = settings_app
        self.setWindowTitle(title)
        self.resize(760, 680)
        self._auto_refresh_enabled: bool = True
        self._build_shell()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def auto_refresh_enabled(self) -> bool:
        return self._auto_refresh_enabled

    def show_and_raise(self) -> None:
        """Show, bring to front, and activate the window."""
        self.show()
        self.raise_()
        self.activateWindow()

    def save_geometry_prefs(self) -> None:
        """Persist current geometry to QSettings."""
        s = self._settings()
        s.setValue(f"{self._settings_app}/geometry", self.saveGeometry())
        s.sync()

    def restore_geometry_prefs(self) -> None:
        """Restore previously saved geometry."""
        s = self._settings()
        geom = s.value(f"{self._settings_app}/geometry")
        if isinstance(geom, QtCore.QByteArray) and not geom.isEmpty():
            self.restoreGeometry(geom)

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def closeEvent(self, event: QtWidgets.QWidget) -> None:  # type: ignore[override]
        """Hide rather than close so the tray icon stays active."""
        event.ignore()
        self.save_geometry_prefs()
        self.hide()

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    def _build_content(self, container: QtWidgets.QWidget) -> None:
        """
        Override in subclasses to populate the main content area.

        *container* is a plain QWidget whose layout is a QVBoxLayout.
        """

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _settings(self) -> QtCore.QSettings:
        return QtCore.QSettings(
            QtCore.QSettings.Format.IniFormat,
            QtCore.QSettings.Scope.UserScope,
            self._settings_org,
            self._settings_app,
        )

    def _build_shell(self) -> None:
        """Build the outer chrome: scroll area, auto-refresh toggle, bottom bar."""
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Auto-refresh toggle
        self._refresh_toggle = QtWidgets.QCheckBox("Auto-refresh")
        self._refresh_toggle.setChecked(True)
        self._refresh_toggle.toggled.connect(self._on_refresh_toggle)
        outer.addWidget(self._refresh_toggle)

        # Scrollable content area
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        self._content_layout = QtWidgets.QVBoxLayout(content)
        self._content_layout.setContentsMargins(4, 4, 4, 4)
        self._content_layout.setSpacing(8)
        self._build_content(content)
        self._content_layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # Bottom bar
        bottom = QtWidgets.QWidget()
        bbar = QtWidgets.QHBoxLayout(bottom)
        bbar.setContentsMargins(0, 4, 0, 0)
        refresh_btn = QtWidgets.QPushButton("Refresh Now")
        refresh_btn.clicked.connect(self.refresh_requested)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.hide)
        bbar.addStretch()
        bbar.addWidget(refresh_btn)
        bbar.addWidget(close_btn)
        outer.addWidget(bottom)

    def _on_refresh_toggle(self, checked: bool) -> None:
        self._auto_refresh_enabled = checked

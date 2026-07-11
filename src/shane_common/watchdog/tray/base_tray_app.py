"""
BaseTrayApp — generic QSystemTrayIcon owner and watchdog tray scaffold.

Responsibilities:
- Owns QSystemTrayIcon, a scaffold context menu (Quit at minimum).
- Poll timer and liveness-write timer.
- Abstract _poll() that subclasses must implement.
- Idempotent _record_app_quit(reason) wired to QApplication.aboutToQuit.
- Persistent prefs via _save_prefs() / _restore_prefs().

Subclass checklist:
1. Override _poll() to read state and update the tray icon.
2. Override _audit_log property to return an AppendOnlyAuditLog instance
   pointing at the correct watchdog audit file.
3. Override _app_id to return the string written into the quit audit record.
4. Optionally override _liveness_payload() for the liveness file content.
5. Optionally call super().start() then do additional setup.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import traceback
from abc import abstractmethod
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtWidgets
from PySide6.QtWidgets import QSystemTrayIcon

from .icons import COLOR_GRAY, make_circle_icon
from ..audit import AppendOnlyAuditLog

log = logging.getLogger(__name__)

_POLL_INTERVAL_MS = 2_000
_LIVENESS_WRITE_INTERVAL_MS = 5_000


def _report(msg: str) -> None:
    try:
        from trading_platform.utils.diag_logger import report_exception  # type: ignore[import]
        report_exception(msg, kind="exception.watchdog.tray")
    except Exception:
        print(f"[watchdog.tray] {msg}")
        traceback.print_exc()


class BaseTrayApp(QtCore.QObject):
    """
    Generic system tray application scaffold for a watchdog observer process.

    Parameters
    ----------
    liveness_path:
        Optional path to write a tray-process liveness JSON file every
        ``liveness_interval_ms`` milliseconds.  Pass None to disable.
    poll_interval_ms:
        How often _poll() is called, in milliseconds.
    liveness_interval_ms:
        How often the liveness file is written, in milliseconds.
    """

    def __init__(
        self,
        liveness_path: Optional[Path] = None,
        poll_interval_ms: int = _POLL_INTERVAL_MS,
        liveness_interval_ms: int = _LIVENESS_WRITE_INTERVAL_MS,
        settings_org: str = "shane_common",
        settings_app: Optional[str] = None,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_org = settings_org
        self._settings_app_name = settings_app  # None → use _app_id at call time

        self._liveness_path = liveness_path
        self._quit_recorded: bool = False

        # Tray icon
        self._tray = QSystemTrayIcon(parent=self)
        self._tray.setIcon(make_circle_icon(COLOR_GRAY))
        self._tray.setToolTip(self._app_id)
        self._tray.activated.connect(self._on_tray_activated)

        # Context menu — subclasses call _build_context_menu() after super().__init__
        # to add items before Quit.
        self._menu = QtWidgets.QMenu()
        self._tray.setContextMenu(self._menu)

        # Quit wired via aboutToQuit so it fires on any exit path.
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(lambda: self._record_app_quit("operator_quit"))
            app.aboutToQuit.connect(self._save_prefs)

        # Poll timer
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(poll_interval_ms)
        self._poll_timer.timeout.connect(self._on_poll_timer)

        # Liveness write timer
        self._liveness_timer = QtCore.QTimer(self)
        self._liveness_timer.setInterval(liveness_interval_ms)
        self._liveness_timer.timeout.connect(self._write_liveness)

    # ------------------------------------------------------------------
    # Subclass interface
    # ------------------------------------------------------------------

    @property
    def _app_id(self) -> str:
        """
        Override to return the unique app_id string used in audit records
        and the liveness file.
        """
        return self.__class__.__name__

    @property
    def _audit_log(self) -> Optional[AppendOnlyAuditLog]:
        """
        Override to return an AppendOnlyAuditLog pointing at the correct
        watchdog audit file.  Return None to disable quit audit recording.
        """
        return None

    @abstractmethod
    def _poll(self) -> None:
        """Read watchdog state from disk and update tray icon / windows."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Show the tray icon and start timers."""
        self._add_quit_to_menu()
        self._tray.show()
        self._poll_timer.start()
        if self._liveness_path is not None:
            self._liveness_timer.start()
        self._restore_prefs()
        QtCore.QTimer.singleShot(0, self._poll)
        if self._liveness_path is not None:
            QtCore.QTimer.singleShot(0, self._write_liveness)

    def request_quit(self, reason: str = "operator_menu") -> None:
        """
        Record the quit reason then ask Qt to quit.

        Route all explicit operator quit actions through this method so the
        audit record captures a specific reason rather than the default
        "operator_quit" emitted by aboutToQuit.
        """
        self._record_app_quit(reason)
        QtWidgets.QApplication.quit()

    # ------------------------------------------------------------------
    # Quit recording (idempotent)
    # ------------------------------------------------------------------

    def _record_app_quit(self, reason: str = "operator_quit") -> None:
        """
        Append a single quit audit record.  Idempotent: subsequent calls
        after the first are silently ignored.
        """
        if self._quit_recorded:
            return
        self._quit_recorded = True

        audit = self._audit_log
        if audit is None:
            return

        record = {
            "ts_wall_utc": time.time(),
            "app_id": self._app_id,
            "event": "app_tray_quit",
            "reason": reason,
            "note": f"{self._app_id}_quit: operator requested tray exit",
        }
        try:
            audit.append(record)
        except Exception:
            _report(f"BaseTrayApp._record_app_quit: audit append failed for {self._app_id!r}")

    # ------------------------------------------------------------------
    # Prefs (QSettings-backed)
    # ------------------------------------------------------------------

    def _settings(self) -> QtCore.QSettings:
        app = self._settings_app_name or self._app_id
        return QtCore.QSettings(
            QtCore.QSettings.Format.IniFormat,
            QtCore.QSettings.Scope.UserScope,
            self._settings_org,
            app,
        )

    def _save_prefs(self) -> None:
        """Override to persist app-specific settings."""

    def _restore_prefs(self) -> None:
        """Override to restore app-specific settings on startup."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _add_quit_to_menu(self) -> None:
        """Append a separator and Quit action to the context menu."""
        self._menu.addSeparator()
        quit_action = self._menu.addAction("Quit")
        quit_action.triggered.connect(
            lambda: self.request_quit("operator_menu")
        )

    def _on_poll_timer(self) -> None:
        try:
            self._poll()
        except Exception:
            _report(f"BaseTrayApp._poll raised an unhandled exception in {self._app_id!r}")

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_show_hide()

    def _on_show_hide(self) -> None:
        """Override to toggle the dashboard window visibility."""

    def _write_liveness(self) -> None:
        """Write the tray liveness file if liveness_path is set."""
        if self._liveness_path is None:
            return
        parent = self._liveness_path.parent
        if not parent.exists():
            return
        try:
            payload = json.dumps(
                self._liveness_payload(),
                separators=(",", ":"),
                sort_keys=True,
            )
            fd, tmp = tempfile.mkstemp(
                prefix=".tray_live_", suffix=".json", dir=parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                try:
                    os.replace(tmp, self._liveness_path)
                except PermissionError:
                    # Windows: destination held open by another process (e.g. supervisor
                    # reading the file).  Skip this tick — next write will succeed.
                    pass
            finally:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
        except Exception:
            _report(f"BaseTrayApp._write_liveness failed for {self._app_id!r}")

    def _liveness_payload(self) -> dict:
        """Build the dict written to the liveness file.  Override to extend."""
        return {
            "app_id": self._app_id,
            "process_id": os.getpid(),
            "ts_wall_utc": time.time(),
        }

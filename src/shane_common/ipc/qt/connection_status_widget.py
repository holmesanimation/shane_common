from typing import Optional, Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from shane_common.ipc.contracts.connection_state import ConnectionState

_DISCONNECTED_STATES = {ConnectionState.DISCONNECTED, ConnectionState.ERROR}
_CONNECTED_STATES    = {ConnectionState.CONNECTED, ConnectionState.DEGRADED}


class ConnectionStatusWidget(QWidget):
    details_requested = Signal()

    def __init__(
        self,
        name: str = "IPC",
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 2, 4, 2)
        self._layout.setSpacing(4)

        self._dot = QFrame(self)
        self._dot.setFixedSize(12, 12)
        self._dot.setStyleSheet("border-radius: 6px; background: #888888;")
        self._layout.addWidget(self._dot)

        self._name_label = QLabel(name, self)
        self._name_label.setStyleSheet("font-weight: bold;")
        self._layout.addWidget(self._name_label)

        self._status_label = QLabel("Disconnected", self)
        self._layout.addWidget(self._status_label)

        self._btn_connect = QPushButton("Connect", self)
        self._btn_connect.setStyleSheet(
            "background: #1a6b3a; color: white; border: none; border-radius: 3px;"
            " padding: 1px 8px; font-size: 10px;"
        )
        self._btn_connect.hide()
        self._layout.addWidget(self._btn_connect)
        if on_connect:
            self._btn_connect.clicked.connect(on_connect)

        self._btn_disconnect = QPushButton("Disconnect", self)
        self._btn_disconnect.setStyleSheet(
            "background: #6b2020; color: white; border: none; border-radius: 3px;"
            " padding: 1px 8px; font-size: 10px;"
        )
        self._btn_disconnect.hide()
        self._layout.addWidget(self._btn_disconnect)
        if on_disconnect:
            self._btn_disconnect.clicked.connect(on_disconnect)

        self._on_connect_cb: Optional[Callable[[], None]] = on_connect
        self._on_disconnect_cb: Optional[Callable[[], None]] = on_disconnect

        self._details_btn = QPushButton("[Details]", self)
        self._details_btn.setFlat(True)
        self._details_btn.setStyleSheet("font-size: 10px; color: #aaaaaa;")
        self._details_btn.clicked.connect(self.details_requested)
        self._layout.addWidget(self._details_btn)

        # Initialise dot + label for the default DISCONNECTED state (buttons stay hidden
        # until process manager calls set_connect_visible / set_disconnect_visible)
        self.set_state(ConnectionState.DISCONNECTED)

    def set_button_labels(self, connect_label: str, disconnect_label: str) -> None:
        """Rename the Connect / Disconnect buttons."""
        self._btn_connect.setText(connect_label)
        self._btn_disconnect.setText(disconnect_label)

    def set_connect_visible(self, visible: bool) -> None:
        """Explicitly show or hide the Connect (Launch) button."""
        self._btn_connect.setVisible(visible)

    def set_disconnect_visible(self, visible: bool) -> None:
        """Explicitly show or hide the Disconnect (Stop) button."""
        self._btn_disconnect.setVisible(visible)

    def set_actions(
        self,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ) -> None:
        """Wire (or replace) the Connect/Disconnect button callbacks after construction."""
        if self._on_connect_cb is not None:
            try:
                self._btn_connect.clicked.disconnect(self._on_connect_cb)
            except RuntimeError:
                pass
        if self._on_disconnect_cb is not None:
            try:
                self._btn_disconnect.clicked.disconnect(self._on_disconnect_cb)
            except RuntimeError:
                pass
        self._on_connect_cb = on_connect
        self._on_disconnect_cb = on_disconnect
        if on_connect:
            self._btn_connect.clicked.connect(on_connect)
        if on_disconnect:
            self._btn_disconnect.clicked.connect(on_disconnect)

    def set_state(self, state: ConnectionState) -> None:
        """Update the dot colour and status label. Does NOT affect button visibility."""
        self._dot.setStyleSheet(f"border-radius: 6px; background: {state.dot_color};")
        self._status_label.setText(state.display_label)

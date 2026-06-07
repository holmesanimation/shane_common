from enum import Enum


class ConnectionState(Enum):
    DISCONNECTED  = "disconnected"
    CONNECTING    = "connecting"
    CONNECTED     = "connected"
    DEGRADED      = "degraded"
    RECONNECTING  = "reconnecting"
    ERROR         = "error"

    @property
    def display_label(self) -> str:
        return self.value.replace("_", " ").title()

    @property
    def dot_color(self) -> str:
        return {
            ConnectionState.DISCONNECTED: "#888888",
            ConnectionState.CONNECTING:   "#FFA500",
            ConnectionState.CONNECTED:    "#00C853",
            ConnectionState.DEGRADED:     "#FFD600",
            ConnectionState.RECONNECTING: "#FFA500",
            ConnectionState.ERROR:        "#D50000",
        }[self]

"""Synchronous framed-JSON WebSocket publisher for a generic event bus.

``WsPublisher`` maintains a map of ``conn_id -> ws`` socket objects and
exposes ``send`` / ``broadcast`` helpers. It is transport-agnostic: callers
pass any socket-like object exposing a synchronous ``ws.send(str)`` /
``ws.close()`` API (e.g. flask-sock). For asyncio-based transports (where
sends must be awaited), implement a small async equivalent locally instead of
forcing this synchronous class to fit.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Optional

_log = logging.getLogger(__name__)


def _is_connection_closed(exc: BaseException) -> bool:
    name = type(exc).__name__
    module = type(exc).__module__
    return (
        name == "ConnectionClosed"
        or module.startswith("simple_websocket")
        or isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError))
    )


class WsPublisher:
    """Thread-safe registry and send helper for active WebSocket connections."""

    def __init__(self) -> None:
        self._connections: Dict[str, Any] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def register_ws(self, conn_id: str, ws: Any) -> None:
        """Register *ws* under *conn_id*."""
        with self._lock:
            self._connections[conn_id] = ws

    def unregister_ws(self, conn_id: str) -> None:
        """Remove *conn_id* from the registry. No-op if absent."""
        with self._lock:
            self._connections.pop(conn_id, None)

    # ------------------------------------------------------------------
    # Send helpers
    # ------------------------------------------------------------------

    def send(self, conn_id: str, event: Any) -> bool:
        """Serialize *event* as JSON and send to the connection *conn_id*.

        Returns ``True`` on success. Returns ``False`` and logs a warning on
        any error (broken connection, serialization failure, etc.).
        """
        with self._lock:
            ws = self._connections.get(conn_id)
        if ws is None:
            return False
        try:
            if hasattr(event, "model_dump_json"):
                payload = event.model_dump_json()
            else:
                payload = json.dumps(event)
            ws.send(payload)
            return True
        except Exception as exc:
            if _is_connection_closed(exc):
                # Normal lifecycle: clients can reconnect while replay/backfill events are in flight.
                _log.info("WsPublisher.send skipped closed connection conn_id=%s: %s", conn_id, exc)
                with self._lock:
                    self._connections.pop(conn_id, None)
                return False
            _log.warning("WsPublisher.send failed for conn_id=%s", conn_id, exc_info=True)
            return False

    def broadcast(self, topic: str, event: Any, registry: Any) -> None:
        """Broadcast *event* to all subscribers of *topic* via *registry*.

        *registry* must be a ``ConnectionRegistry`` instance.
        """
        conn_ids = registry.subscribers(topic)
        for conn_id in conn_ids:
            self.send(conn_id, event)

    def broadcast_raw(self, msg: dict) -> None:
        """Serialize *msg* as JSON and send to all registered connections.

        Bypasses the ring buffer and subscription registry. Used for
        protocol-level messages such as heartbeats that are not domain events.
        """
        with self._lock:
            conn_ids = list(self._connections.keys())
        payload = json.dumps(msg)
        for conn_id in conn_ids:
            with self._lock:
                ws = self._connections.get(conn_id)
            if ws is None:
                continue
            try:
                ws.send(payload)
            except Exception:
                _log.debug("WsPublisher.broadcast_raw: send failed for conn_id=%s", conn_id)

    def disconnect_all(self) -> None:
        """Close all active WebSocket connections (kick all clients)."""
        with self._lock:
            sockets = list(self._connections.values())
        for ws in sockets:
            try:
                ws.close()
            except Exception:
                pass

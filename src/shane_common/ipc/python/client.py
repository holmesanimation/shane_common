# shane_common/src/shane_common/ipc/python/client.py
"""ApiClient — WebSocket + HTTP client for the trading platform API bus.

Connects to a running ``ApiServer`` (e.g. started via
``python -m trading_platform.api.host``), handles authentication, sends
subscribe / unsubscribe messages, and dispatches inbound events to
registered handlers.

WebSocket transport uses the ``websockets`` async library run inside a
dedicated background thread that owns its own asyncio event loop.
HTTP transport uses ``requests``.

Reconnect behaviour: on connection drop the client retries with exponential
back-off (0.5 s base, ×2 per attempt, capped at 30 s).  On reconnect all
active subscriptions are re-sent with the last seen ``seq`` so lossless
topics resume without gaps.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from shane_common.ipc.python.traffic_recorder import TrafficRecorder
    from shane_common.ipc.contracts.connection_state import ConnectionState

_log = logging.getLogger("IpcClient")


class ApiClient:
    """Client for the trading platform API bus.

    Parameters
    ----------
    host:
        Hostname of the API server.
    port:
        Port of the API server.
    client_name:
        Identifies this client in hello handshake.
    client_version:
        Version string sent in hello handshake.
    traffic_recorder:
        Optional :class:`TrafficRecorder` instance; if set, all inbound and
        outbound WS messages are recorded.
    on_state_changed:
        Optional callback fired on connection state transitions.  Receives a
        :class:`ConnectionState` value.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5001,
        client_name: str = "unknown",
        client_version: str = "unknown",
        traffic_recorder: Optional["TrafficRecorder"] = None,
        on_state_changed: Optional[Callable[["ConnectionState"], None]] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._client_name = client_name
        self._client_version = client_version
        self._traffic_recorder = traffic_recorder
        self._on_state_changed = on_state_changed
        self._secret: Optional[str] = None

        self._base_url = f"http://{host}:{port}"
        self._ws_base_url = f"ws://{host}:{port}/ws/events"

        # Async infrastructure
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws: Any = None  # active websockets.ClientConnection

        # Subscription state
        # topic → {"handlers": [Callable], "filters": dict, "last_seq": int}
        self._subscriptions: Dict[str, Dict[str, Any]] = {}
        self._subscriptions_lock = threading.Lock()

        # All handlers (all topics) — called for every inbound event.
        # Simpler than per-topic routing since ApiEvent has no "topic" field.
        self._all_handlers: List[Callable[[dict], None]] = []
        self._all_handlers_lock = threading.Lock()

        # Lifecycle
        self._connected_event = threading.Event()
        self._reconnect = False

        # Hello handshake state
        self._session_id: Optional[str] = None
        self._server_protocol_version: Optional[int] = None

        # Heartbeat tracking
        self._last_heartbeat_ts: float = 0.0

        # Ping/pong (threading.Event allows sync wait from outside the loop)
        self._pong_event = threading.Event()
        self._pong_nonce: Optional[str] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fire_state(self, state_value: str) -> None:
        """Fire on_state_changed with the matching ConnectionState enum member."""
        if self._on_state_changed is None:
            return
        try:
            from shane_common.ipc.contracts.connection_state import ConnectionState
            self._on_state_changed(ConnectionState(state_value))
        except Exception:
            import traceback
            traceback.print_exc()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Read the loopback secret and open the WebSocket connection.

        Blocks until the initial connection is established (up to 10 s).
        Raises ``RuntimeError`` if the connection cannot be made.
        """
        from trading_platform.api.server.auth import (
            loopback_secret_path,
            read_shared_secret,
        )

        self._secret = read_shared_secret(loopback_secret_path())
        self._reconnect = True
        self._connected_event.clear()

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name="ApiClient-WS",
            daemon=True,
        )
        self._thread.start()

        if not self._connected_event.wait(timeout=10.0):
            _log.warning("ApiClient: timed out waiting for initial connection")

    def disconnect(self) -> None:
        """Close the WebSocket and stop the background thread."""
        self._reconnect = False
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def get(self, path: str, **params) -> dict:
        """HTTP GET with bearer auth.  Returns the parsed JSON response."""
        import requests

        url = f"{self._base_url}{path}"
        resp = requests.get(
            url,
            params=params or None,
            headers={"Authorization": f"Bearer {self._secret}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, body: dict) -> dict:
        """HTTP POST with bearer auth.  Returns the parsed JSON response."""
        import requests

        url = f"{self._base_url}{path}"
        resp = requests.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {self._secret}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(
        self,
        topic: str,
        handler: Callable[[dict], None],
        filters: Optional[dict] = None,
        last_seq: Optional[int] = None,
    ) -> None:
        """Subscribe to *topic* and register *handler* for inbound events.

        *handler* is called from the background reader thread with each
        received event dict.  All registered handlers receive every inbound
        event regardless of topic; the handler is responsible for filtering
        by ``kind`` if needed.

        If already connected, sends the subscribe message immediately.
        The subscription is re-sent on reconnect with the last seen ``seq``.
        """
        filters = filters or {}
        with self._subscriptions_lock:
            self._subscriptions[topic] = {
                "filters": filters,
                "last_seq": last_seq or 0,
            }
        with self._all_handlers_lock:
            if handler not in self._all_handlers:
                self._all_handlers.append(handler)

        # Send immediately if the loop is running
        if self._loop is not None and not self._loop.is_closed() and self._ws is not None:
            msg = {
                "op": "subscribe",
                "topic": topic,
                "filters": filters,
                "last_seq": last_seq or 0,
            }
            asyncio.run_coroutine_threadsafe(self._send_json(msg), self._loop)

    def unsubscribe(self, topic: str) -> None:
        """Unsubscribe from *topic*."""
        with self._subscriptions_lock:
            self._subscriptions.pop(topic, None)
        if self._loop is not None and not self._loop.is_closed() and self._ws is not None:
            msg = {"op": "unsubscribe", "topic": topic}
            asyncio.run_coroutine_threadsafe(self._send_json(msg), self._loop)

    # ------------------------------------------------------------------
    # Async internals
    # ------------------------------------------------------------------

    def _run_event_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_loop())
        except Exception:
            _log.exception("ApiClient: event loop crashed")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _connect_loop(self) -> None:
        """Outer reconnect loop with exponential back-off."""
        import websockets

        delay = 0.5
        while self._reconnect:
            ws_url = f"{self._ws_base_url}?token={self._secret}"
            self._fire_state("connecting")
            try:
                async with websockets.connect(ws_url) as ws:
                    self._ws = ws
                    _log.info("ApiClient: connected to %s", self._ws_base_url)
                    delay = 0.5  # reset back-off on success

                    # Hello handshake (non-fatal timeout)
                    await self._send_json({
                        "op": "hello",
                        "client_name": self._client_name,
                        "client_version": self._client_version,
                        "protocol_version": 1,
                    })
                    try:
                        raw_ack = await asyncio.wait_for(ws.recv(), timeout=3.0)
                        ack_data = json.loads(raw_ack)
                        if ack_data.get("op") == "hello.ack":
                            self._session_id = ack_data.get("session_id")
                            self._server_protocol_version = ack_data.get("protocol_version")
                            _log.info(
                                "ApiClient: hello.ack received session_id=%s",
                                self._session_id,
                            )
                        else:
                            _log.warning(
                                "ApiClient: expected hello.ack, got op=%s; continuing",
                                ack_data.get("op"),
                            )
                            self._dispatch(raw_ack)
                    except asyncio.TimeoutError:
                        _log.warning("ApiClient: hello.ack timeout — continuing without handshake")
                    except Exception as exc:
                        _log.warning("ApiClient: hello handshake error: %s", exc)

                    self._fire_state("connected")
                    self._connected_event.set()

                    await self._resubscribe_all()

                    async for raw_message in ws:
                        if not self._reconnect:
                            break
                        self._dispatch(raw_message)

            except Exception as exc:
                _log.warning(
                    "ApiClient: connection lost (%s); retrying in %.1f s",
                    exc,
                    delay,
                )
                self._ws = None
                if not self._reconnect:
                    break
                self._fire_state("reconnecting")
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 30.0)

        self._ws = None
        self._fire_state("disconnected")
        _log.info("ApiClient: disconnected")

    async def _resubscribe_all(self) -> None:
        """Re-send all active subscriptions after a reconnect."""
        with self._subscriptions_lock:
            subs = dict(self._subscriptions)
        for topic, info in subs.items():
            msg = {
                "op": "subscribe",
                "topic": topic,
                "filters": info["filters"],
                "last_seq": info["last_seq"],
            }
            await self._send_json(msg)

    async def _send_json(self, msg: dict) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps(msg))
                if self._traffic_recorder is not None:
                    self._traffic_recorder.record("outgoing", msg)
            except Exception as exc:
                _log.debug("ApiClient: send failed: %s", exc)

    def _dispatch(self, raw: str) -> None:
        """Parse an inbound WS message and route to all registered handlers."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            _log.debug("ApiClient: received non-JSON message")
            return

        if self._traffic_recorder is not None:
            self._traffic_recorder.record("incoming", data)

        # Handle protocol-level ops — do not pass to application handlers
        op = data.get("op")
        if op == "hello.ack":
            self._session_id = data.get("session_id")
            self._server_protocol_version = data.get("protocol_version")
            _log.info("ApiClient: hello.ack received session_id=%s", self._session_id)
            return
        if op == "pong":
            if data.get("nonce") == self._pong_nonce:
                self._pong_event.set()
            return
        if op == "heartbeat":
            self._last_heartbeat_ts = data.get("ts", time.time())
            return

        # Update last_seq for the relevant subscription (match by kind prefix)
        seq = data.get("seq", 0)
        kind = data.get("kind", "")
        with self._subscriptions_lock:
            for topic, info in self._subscriptions.items():
                # A topic like "decision" matches kind "decision.entry"
                if kind == topic or kind.startswith(topic + ".") or topic in kind:
                    if seq > info.get("last_seq", 0):
                        info["last_seq"] = seq
                    break

        # Dispatch to all handlers
        with self._all_handlers_lock:
            handlers = list(self._all_handlers)
        for handler in handlers:
            try:
                handler(data)
            except Exception:
                import traceback
                traceback.print_exc()

    # ------------------------------------------------------------------
    # Ping / heartbeat / session
    # ------------------------------------------------------------------

    def send_ping(self) -> Optional[float]:
        """Send a ping and block up to 2 s for the pong reply.

        Returns round-trip latency in milliseconds, or ``None`` if the ping
        could not be sent or no pong was received within the timeout.
        """
        import uuid as _uuid
        if self._loop is None or self._loop.is_closed() or self._ws is None:
            return None
        nonce = str(_uuid.uuid4())
        self._pong_event.clear()
        self._pong_nonce = nonce
        t0 = time.time()
        future = asyncio.run_coroutine_threadsafe(
            self._send_json({"op": "ping", "nonce": nonce}),
            self._loop,
        )
        try:
            future.result(timeout=2.0)
        except Exception as exc:
            _log.debug("ApiClient: send_ping send failed: %s", exc)
            return None
        if self._pong_event.wait(timeout=2.0):
            return (time.time() - t0) * 1000.0
        return None

    @property
    def last_heartbeat_ts(self) -> float:
        """Unix timestamp of the last received heartbeat (0.0 if none yet)."""
        return self._last_heartbeat_ts

    def check_degraded(self, threshold_s: float = 10.0) -> bool:
        """Return True if connected but no heartbeat received within *threshold_s* seconds."""
        from shane_common.ipc.contracts.connection_state import ConnectionState
        if self._last_heartbeat_ts == 0.0:
            return False
        return (time.time() - self._last_heartbeat_ts) > threshold_s

    @property
    def session_id(self) -> Optional[str]:
        """Session ID assigned by the server in hello.ack, or None."""
        return self._session_id

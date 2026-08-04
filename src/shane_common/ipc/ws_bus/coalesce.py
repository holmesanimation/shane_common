"""Topic coalescer for lossy WS event-bus lanes.

For high-frequency lossy topics the server coalesces updates by ``filter_key`` so
that only the latest event per key is forwarded to subscribers. A periodic timer
calls ``flush()`` and broadcasts whatever has accumulated.

Framework/event-shape neutral: stores whatever event object callers pass in.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List


class TopicCoalescer:
    """Keeps only the most-recent event per ``filter_key``.

    ``update(filter_key, event)`` stores the event, overwriting any previous
    value for that key. ``flush()`` atomically drains the store and returns
    all current latest-per-key events, then clears the internal state.

    Thread-safe: all public methods are guarded by an internal lock.
    """

    def __init__(self) -> None:
        self._store: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def update(self, filter_key: str, event: Any) -> None:
        """Record *event* as the latest for *filter_key*, discarding any prior."""
        with self._lock:
            self._store[filter_key] = event

    def flush(self) -> List[Any]:
        """Atomically drain and return all latest-per-key events.

        After this call the internal store is empty.
        """
        with self._lock:
            events = list(self._store.values())
            self._store.clear()
        return events

"""Per-connection subscription registry for a generic WS event bus.

Each WebSocket connection owns an independent subscription set. High-volume
topics can require a filter to prevent accidental fan-out of every event to every
client.

Design notes
------------
- ``register`` / ``unregister`` are called on connect / disconnect.
- ``subscribe`` stores ``(topic, filters)`` for a connection.
- ``subscribers`` returns the list of connection IDs that have subscribed to
  a given topic, optionally filtered by a ``filter_key`` that must appear in
  their recorded ``filters`` dict (empty filters dict = match all keys).
- Topics passed in ``filter_required_topics`` at construction time raise
  ``ValueError`` on unfiltered subscribe attempts so mis-configured clients fail
  fast. Topic naming/business logic (which topics require filters) is caller-owned;
  this class has no built-in topic list.
"""
from __future__ import annotations

import threading
from typing import Dict, FrozenSet, List, Optional


class ConnectionRegistry:
    """Thread-safe registry of WS connections and their subscriptions.

    Parameters
    ----------
    filter_required_topics:
        Topics that mandate at least one filter key on subscription. Callers
        that need this behavior pass their own domain-specific topic set;
        defaults to empty (no topic requires a filter).
    """

    def __init__(self, filter_required_topics: FrozenSet[str] = frozenset()) -> None:
        # conn_id -> {topic -> filters_dict}
        self._subs: Dict[str, Dict[str, Dict[str, str]]] = {}
        self._lock = threading.Lock()
        self._filter_required_topics = filter_required_topics

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def register(self, conn_id: str) -> None:
        """Register a new connection with an empty subscription set."""
        with self._lock:
            self._subs.setdefault(conn_id, {})

    def unregister(self, conn_id: str) -> None:
        """Remove a connection and all its subscriptions."""
        with self._lock:
            self._subs.pop(conn_id, None)

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    def subscribe(
        self,
        conn_id: str,
        topic: str,
        filters: Optional[Dict[str, str]] = None,
    ) -> None:
        """Subscribe *conn_id* to *topic* with optional *filters*.

        Raises
        ------
        ValueError
            If *topic* is in ``filter_required_topics`` and *filters* is empty
            or ``None``.
        KeyError
            If *conn_id* has not been registered.
        """
        filters = dict(filters) if filters else {}
        if topic in self._filter_required_topics and not filters:
            raise ValueError(
                f"Topic '{topic}' requires at least one filter key; "
                "pass a non-empty filters dict."
            )
        with self._lock:
            if conn_id not in self._subs:
                raise KeyError(f"Connection '{conn_id}' is not registered.")
            self._subs[conn_id][topic] = filters

    def unsubscribe(self, conn_id: str, topic: str) -> None:
        """Remove the subscription for *conn_id* / *topic*.

        No-op if the connection or topic is not present.
        """
        with self._lock:
            if conn_id in self._subs:
                self._subs[conn_id].pop(topic, None)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def subscribers(
        self,
        topic: str,
        filter_key: Optional[str] = None,
    ) -> List[str]:
        """Return all connection IDs subscribed to *topic*.

        If *filter_key* is provided, only connections whose stored filters
        dict is empty (meaning "all keys") **or** contains *filter_key* are
        returned.
        """
        result: List[str] = []
        with self._lock:
            for conn_id, subs in self._subs.items():
                if topic not in subs:
                    continue
                stored_filters = subs[topic]
                if filter_key is None:
                    result.append(conn_id)
                elif not stored_filters or filter_key in stored_filters.values():
                    result.append(conn_id)
        return result

    def active_count(self) -> int:
        """Return the number of currently registered connections."""
        with self._lock:
            return len(self._subs)

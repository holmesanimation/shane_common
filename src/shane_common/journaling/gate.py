"""Event gate helpers — emit_on_change, emit_once, throttle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict


@dataclass
class EventGate:
    """
    State holder for emission suppression.
    Each owning component should keep its own EventGate.
    """
    _last_sig_by_key: Dict[str, str] = field(default_factory=dict)
    _once_tokens_by_key: Dict[str, str] = field(default_factory=dict)
    _last_ts_by_key: Dict[str, float] = field(default_factory=dict)

    def emit_on_change(self, *, key: str, signature: str, emit_fn: Callable[[], None]) -> bool:
        prev = self._last_sig_by_key.get(key)
        if prev == signature:
            return False
        self._last_sig_by_key[key] = signature
        emit_fn()
        return True

    def emit_once(self, *, key: str, token: str, emit_fn: Callable[[], None]) -> bool:
        prev = self._once_tokens_by_key.get(key)
        if prev == token:
            return False
        self._once_tokens_by_key[key] = token
        emit_fn()
        return True

    def throttle(
        self,
        *,
        key: str,
        now_ts: float,
        min_interval_s: float,
        emit_fn: Callable[[], None],
    ) -> bool:
        last = float(self._last_ts_by_key.get(key, 0.0) or 0.0)
        if (now_ts - last) < float(min_interval_s):
            return False
        self._last_ts_by_key[key] = float(now_ts)
        emit_fn()
        return True

from __future__ import annotations

import hashlib
import json
import math
import time
import traceback
from dataclasses import asdict
from typing import Callable, Dict, Optional, Tuple

from shane_common.notify.contracts import NotificationEvent, NotificationSeverity
from shane_common.notify.adapters.base import INotifyAdapter


class NotifyManager:
    """
    Shared paging/event notification manager.

    Responsibilities:
      - idempotency (stable event_id hashing)
      - anti-spam windowing per logical key
      - adapter delivery best-effort (never raise)

    Parameters
    ----------
    adapter:
        INotifyAdapter implementation for delivery (Bark, Telegram, Composite, Null).
    clock_fn:
        Optional callable returning the current time as a float (epoch seconds).
        Used when ``ts`` is not supplied to notify_emit/notify_info/notify_warning/
        notify_urgent.  Falls back to ``time.time()`` when None.
    enabled:
        Global on/off switch.  When False, all emit calls are no-ops.
    min_emit_interval_s_default / _warning / _urgent:
        Per-severity minimum interval between re-emissions of the same logical
        event key, in seconds.
    """

    def __init__(
        self,
        *,
        adapter: INotifyAdapter,
        clock_fn: Callable[[], float] | None = None,
        min_emit_interval_s_default: float = 60.0,
        min_emit_interval_s_warning: float = 120.0,
        min_emit_interval_s_urgent: float = 180.0,
        enabled: bool = True,
    ) -> None:
        self._adapter = adapter
        self._clock_fn = clock_fn
        self._enabled = bool(enabled)

        self._min_default = float(min_emit_interval_s_default)
        self._min_warning = float(min_emit_interval_s_warning)
        self._min_urgent = float(min_emit_interval_s_urgent)

        # Windowing + idempotency memory (in-process)
        self._last_emit_ts_by_window: Dict[Tuple[str, str, str, str, str, str], float] = {}
        self._sent_event_ids: Dict[str, float] = {}

    def emit(self, event: NotificationEvent) -> Optional[str]:
        """
        Returns event_id if delivered/accepted for delivery, else None
        (suppressed or disabled).
        """
        if not self._enabled:
            return None

        try:
            event_id = self._event_id(event)
            now_ts = float(event.ts)

            # Hard idempotency: same event_id only once per process
            if event_id in self._sent_event_ids:
                return None

            window_key = self._window_key(event)
            min_interval_s = self._min_interval_for(event.severity)

            last_ts = float(self._last_emit_ts_by_window.get(window_key, -1e30))
            if (now_ts - last_ts) < min_interval_s:
                return None

            self._last_emit_ts_by_window[window_key] = now_ts
            self._sent_event_ids[event_id] = now_ts

            # Adapter must never raise, but we still guard.
            try:
                self._adapter.send(event)
            except Exception:
                traceback.print_exc()
                return None

            return event_id
        except Exception:
            traceback.print_exc()
            return None

    # ------------------------------------------------------------------
    # App-facing helpers (best-effort; never raise)
    # ------------------------------------------------------------------

    def notify_emit(
        self,
        *,
        kind: str,
        scope: str,
        severity: NotificationSeverity = NotificationSeverity.INFO,
        instrument: str | None = None,
        strategy_id: str | None = None,
        details: str | None = None,
        data: dict | None = None,
        dedupe_key: str | None = None,
        ts: float | None = None,
    ) -> str | None:
        """Best-effort push notification.

        Anti-spam + idempotency are owned by NotifyManager.
        Do NOT include secrets in details/data.
        """
        try:
            if ts is not None:
                now_ts = float(ts)
            else:
                now_ts = self._now()

            evt = NotificationEvent(
                ts=float(now_ts),
                kind=str(kind or ""),
                scope=str(scope or ""),
                severity=severity,
                instrument=str(instrument or "") or None,
                strategy_id=str(strategy_id or "") or None,
                details=str(details or "") or None,
                data=dict(data or {}) or None,
                dedupe_key=str(dedupe_key or "") or None,
            )
            return self.emit(evt)
        except Exception:
            traceback.print_exc()
            return None

    def notify_info(
        self,
        *,
        kind: str,
        scope: str,
        instrument: str | None = None,
        strategy_id: str | None = None,
        details: str | None = None,
        data: dict | None = None,
        dedupe_key: str | None = None,
        ts: float | None = None,
    ) -> str | None:
        return self.notify_emit(
            kind=kind,
            scope=scope,
            severity=NotificationSeverity.INFO,
            instrument=instrument,
            strategy_id=strategy_id,
            details=details,
            data=data,
            dedupe_key=dedupe_key,
            ts=ts,
        )

    def notify_warning(
        self,
        *,
        kind: str,
        scope: str,
        instrument: str | None = None,
        strategy_id: str | None = None,
        details: str | None = None,
        data: dict | None = None,
        dedupe_key: str | None = None,
        ts: float | None = None,
    ) -> str | None:
        return self.notify_emit(
            kind=kind,
            scope=scope,
            severity=NotificationSeverity.WARNING,
            instrument=instrument,
            strategy_id=strategy_id,
            details=details,
            data=data,
            dedupe_key=dedupe_key,
            ts=ts,
        )

    def notify_urgent(
        self,
        *,
        kind: str,
        scope: str,
        instrument: str | None = None,
        strategy_id: str | None = None,
        details: str | None = None,
        data: dict | None = None,
        dedupe_key: str | None = None,
        ts: float | None = None,
    ) -> str | None:
        return self.notify_emit(
            kind=kind,
            scope=scope,
            severity=NotificationSeverity.URGENT,
            instrument=instrument,
            strategy_id=strategy_id,
            details=details,
            data=data,
            dedupe_key=dedupe_key,
            ts=ts,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _now(self) -> float:
        if self._clock_fn is not None:
            try:
                t = self._clock_fn()
                if t is not None:
                    return float(t)
            except Exception:
                pass
        return time.time()

    def _stable_dedupe_key(self, event: NotificationEvent) -> str:
        """
        Stable dedupe identity for idempotency + anti-spam.

        Priority:
        1) event.dedupe_key (preferred)
        2) event.data["dedupe_key"] (compat)
        3) empty string (fallback)
        """
        try:
            if getattr(event, "dedupe_key", None):
                return str(event.dedupe_key or "")
        except Exception:
            pass

        try:
            if event.data and isinstance(event.data, dict):
                dk = event.data.get("dedupe_key", None)
                if dk:
                    return str(dk)
        except Exception:
            pass

        return ""

    def _min_interval_for(self, sev: NotificationSeverity) -> float:
        if sev == NotificationSeverity.URGENT:
            return self._min_urgent
        if sev == NotificationSeverity.WARNING:
            return self._min_warning
        return self._min_default

    def _window_key(self, event: NotificationEvent) -> Tuple[str, str, str, str, str, str]:
        """
        Anti-spam window key:
        (kind, scope, strategy_id, instrument, severity, dedupe_key)
        """
        return (
            str(event.kind or ""),
            str(event.scope or ""),
            str(event.strategy_id or ""),
            str(event.instrument or ""),
            str(event.severity.value),
            str(self._stable_dedupe_key(event)),
        )

    def _event_id(self, event: NotificationEvent) -> str:
        """
        Hard idempotency hash.

        IMPORTANT:
        - MUST be stable across repeated emits of "the same problem"
        - MUST NOT include event.ts (defeats idempotency)
        - Relies on dedupe_key to distinguish cases

        We intentionally do NOT hash details/data to avoid accidental churn.
        """
        payload = {
            "kind": str(event.kind or ""),
            "scope": str(event.scope or ""),
            "severity": str(event.severity.value),
            "strategy_id": str(event.strategy_id or ""),
            "instrument": str(event.instrument or ""),
            "dedupe_key": str(self._stable_dedupe_key(event)),
        }
        normalized = _sanitize(payload)
        blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _sanitize(x: object) -> object:
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return None
        return float(x)
    if isinstance(x, dict):
        return {str(k): _sanitize(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_sanitize(v) for v in x]
    return x

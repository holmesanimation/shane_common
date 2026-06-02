# shane_common/ui/log_viewer/base_normalizer_sink.py
"""
BaseLogNormalizerSink — abstract base for log normalisation sinks.

Provides all shared normalisation logic (kind/ts parsing, spec lookup,
severity/message/problem resolution, dedupe) so concrete subclasses only
need to supply a ``kind_spec_fn`` and an emitter, plus an optional
``_post_emit`` hook for side-effects (CSV, API publish, etc.).
"""
from __future__ import annotations

import threading
import time
import traceback
from abc import ABC
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shane_common.ui.log_viewer.kind_spec import (
    KindSpecResolver,
    _DEFAULT_KIND_SPEC,
    _format_template,
)
from shane_common.ui.log_viewer.log_row import LogRow
from shane_common.ui.log_viewer.log_row_emitter import LogRowEmitterProtocol


# ------------------------------------------------------------------ #
# Internal bookkeeping
# ------------------------------------------------------------------ #

@dataclass
class _DedupeEntry:
    """Mutable state for a single dedupe key."""
    last_emit_ts: float
    count: int = 0
    last_suppressed_ts: Optional[float] = None


# ------------------------------------------------------------------ #
# BaseLogNormalizerSink
# ------------------------------------------------------------------ #

class BaseLogNormalizerSink(ABC):
    """
    Abstract base class for log normalisation sinks.

    Subclass and provide:
    - ``kind_spec_fn`` — maps a kind string to ``KindSpec | None``
    - ``emitter`` — any object satisfying ``LogRowEmitterProtocol``
    - Override ``_post_emit(row)`` for CSV, API publishing, etc.
    """

    def __init__(
        self,
        kind_spec_fn: KindSpecResolver,
        emitter: LogRowEmitterProtocol,
    ) -> None:
        self._kind_spec_fn = kind_spec_fn
        self._emitter = emitter
        self._lock = threading.Lock()
        self._dedupe_state: Dict[tuple, _DedupeEntry] = {}
        # Consecutive-message dedupe: (kind, instrument) -> last emitted message.
        self._last_message: Dict[tuple, str] = {}

    # ------------------------------------------------------------------ #
    # Sink interface
    # ------------------------------------------------------------------ #

    def emit(self, envelope: Dict[str, Any]) -> None:
        """
        Normalise *envelope* → ``LogRow`` and forward to the emitter.

        Never raises — callers (journaling services) must not be crashed.
        """
        try:
            self._emit_inner(envelope)
        except Exception:
            traceback.print_exc()

    def close(self) -> None:
        """No-op close; subclasses may override to release resources."""

    # ------------------------------------------------------------------ #
    # Internal normalisation
    # ------------------------------------------------------------------ #

    def _emit_inner(self, envelope: Dict[str, Any]) -> None:
        # Capture wall-clock time immediately.
        wall_clock_ts = time.time()

        kind = str(envelope.get("kind") or "")
        if not kind:
            return

        spec = self._kind_spec_fn(kind)
        if spec is None:
            spec = _DEFAULT_KIND_SPEC

        payload = envelope.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        # ---- timestamp ------------------------------------------------ #
        ts_epoch = _parse_ts(envelope.get("ts"))
        if ts_epoch is None:
            return  # unusable envelope

        # ---- instrument ----------------------------------------------- #
        instrument = envelope.get("instrument")
        if instrument in (None, "", "_"):
            instrument = None

        # ---- severity (static or conditional) ------------------------- #
        if spec.severity_fn is not None:
            severity = spec.severity_fn(payload)
        else:
            severity = spec.severity

        # ---- problem (static or conditional) -------------------------- #
        if spec.problem_fn is not None:
            problem = spec.problem_fn(payload)
        else:
            problem = spec.problem

        # ---- message -------------------------------------------------- #
        if spec.message_fn is not None:
            message = spec.message_fn(payload)
        else:
            message = _format_template(spec.message_template, payload, envelope)

        # ---- details -------------------------------------------------- #
        details: Dict[str, Any] = dict(payload)
        correlation = envelope.get("correlation")
        if isinstance(correlation, dict):
            details.update(correlation)

        # ---- source file --------------------------------------------- #
        source_file: str | None = envelope.get("_source_file") or None

        # ---- consecutive-message dedupe ------------------------------- #
        if spec.dedupe_until_changed:
            msg_key = (kind, instrument)
            with self._lock:
                if self._last_message.get(msg_key) == message:
                    return
                self._last_message[msg_key] = message

        # ---- hard dedupe ---------------------------------------------- #
        dedupe_count: Optional[int] = None
        dedupe_last_ts: Optional[float] = None

        if spec.dedupe_interval_s is not None:
            disc_val: Optional[str] = None
            if spec.dedupe_discriminator is not None:
                disc_val = spec.dedupe_discriminator(payload)
            dedupe_key = (kind, instrument, disc_val)

            suppressed, dedupe_count, dedupe_last_ts = self._check_dedupe(
                dedupe_key, ts_epoch, spec.dedupe_interval_s,
            )
            if suppressed:
                return

        # ---- build LogRow --------------------------------------------- #
        row = LogRow(
            ts=ts_epoch,
            kind=kind,
            severity=severity,
            code=kind,
            instrument=instrument,
            message=message,
            details=details,
            importance=spec.importance,
            problem=problem,
            dedupe_count=dedupe_count,
            dedupe_last_ts=dedupe_last_ts,
            wall_clock_ts=wall_clock_ts,
            source_file=source_file,
        )

        self._emitter.emit_row(row)
        self._post_emit(row)

    # ------------------------------------------------------------------ #
    # Post-emit hook (override for side-effects)
    # ------------------------------------------------------------------ #

    def _post_emit(self, row: LogRow) -> None:
        """Called after each emitted row. No-op by default; override for CSV etc."""

    # ------------------------------------------------------------------ #
    # Hard dedupe
    # ------------------------------------------------------------------ #

    def _check_dedupe(
        self,
        key: tuple,
        ts: float,
        interval_s: float,
    ) -> tuple[bool, Optional[int], Optional[float]]:
        """
        Check and update hard-dedupe state.

        Returns ``(suppressed, count, last_ts)``.
        """
        with self._lock:
            entry = self._dedupe_state.get(key)

            if entry is None:
                self._dedupe_state[key] = _DedupeEntry(last_emit_ts=ts)
                return (False, None, None)

            elapsed = ts - entry.last_emit_ts
            if elapsed < interval_s:
                entry.count += 1
                entry.last_suppressed_ts = ts
                return (True, None, None)

            count = entry.count if entry.count > 0 else None
            last_ts = entry.last_suppressed_ts

            entry.last_emit_ts = ts
            entry.count = 0
            entry.last_suppressed_ts = None

            return (False, count, last_ts)

    def reset(self) -> None:
        """Clear all dedupe state (useful for replay seek)."""
        with self._lock:
            self._dedupe_state.clear()
            self._last_message.clear()

    def drain_buffer(self) -> List[LogRow]:
        """Return a snapshot of all buffered rows in chronological order."""
        return self._emitter.drain_buffer()


# ------------------------------------------------------------------ #
# Helpers (module-private)
# ------------------------------------------------------------------ #

def _parse_ts(raw: Any) -> Optional[float]:
    """Parse envelope ``ts`` (ISO-8601 string or numeric) to epoch float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            traceback.print_exc()
            return None
    return None

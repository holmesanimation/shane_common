"""Shared journal service for shane_common journaling."""

from __future__ import annotations

import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from shane_common.journaling.envelope import JournalSource, build_journal_envelope
from shane_common.journaling.profile import JournalProfile


_log_ctx: ContextVar[Dict[str, Optional[str]]] = ContextVar(
    "journal_log_context",
    default={},
)


class JournalContext:
    @staticmethod
    def get() -> Dict[str, Optional[str]]:
        return dict(_log_ctx.get())

    @staticmethod
    def bind(**kwargs: Optional[str]) -> None:
        ctx = dict(_log_ctx.get())
        for k, v in kwargs.items():
            if v is not None:
                ctx[k] = str(v)
        _log_ctx.set(ctx)

    @staticmethod
    @contextmanager
    def scope(**kwargs: Optional[str]):
        token = _log_ctx.set(
            {**_log_ctx.get(), **{k: str(v) for k, v in kwargs.items() if v is not None}}
        )
        try:
            yield
        finally:
            _log_ctx.reset(token)


@dataclass(frozen=True)
class JournalConfig:
    app_id: str
    app_name: str
    run_id: str


class JournalService:
    def __init__(
        self,
        cfg: JournalConfig,
        profile: JournalProfile,
        sinks: List[Any],
        clock: Optional[Any] = None,
        logger: Optional[Any] = None,
    ) -> None:
        self.cfg = cfg
        self._profile = profile
        self._sinks = list(sinks or [])
        self._clock = clock
        self._log = logger
        self._last_emit_ts: Optional[float] = None
        self._emit_error_count: int = 0

    def last_emit_ts(self) -> Optional[float]:
        return self._last_emit_ts

    def emit_error_count(self) -> int:
        return self._emit_error_count

    def emit(
        self,
        kind: str,
        payload: Dict[str, Any],
        *,
        source: str = "unknown",
        msg: Optional[str] = None,
        correlation: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            app_clock_ts: Optional[float] = None
            if self._clock is not None:
                try:
                    clock_now = float(self._clock.now_ts or 0.0)
                    if clock_now > 0.0:
                        app_clock_ts = clock_now
                except Exception:
                    pass

            src = JournalSource(app=self.cfg.app_id, component=str(source))

            env = build_journal_envelope(
                kind=kind,
                run_id=self.cfg.run_id,
                source=src,
                payload=payload,
                app_clock_ts=app_clock_ts,
                msg=msg,
                correlation=correlation,
                profile=self._profile,
            )

            for sink in self._sinks:
                try:
                    sink.emit(env)
                except Exception:
                    traceback.print_exc()

            self._last_emit_ts = time.time()

        except Exception:
            self._emit_error_count += 1
            traceback.print_exc()

    def close_sinks(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:
                traceback.print_exc()

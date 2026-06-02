# shane_common/logging/logging_utils.py
"""
AlignedFormatter and get_logger — shared structured logging utilities.

See shane_common/docs/logging.md for full usage guide.
"""
from __future__ import annotations

import logging
import sys
from typing import Any


class AlignedFormatter(logging.Formatter):
    """Formatter that left-aligns the log level to 8 characters and safely renders optional fields."""

    def format(self, record: logging.LogRecord) -> str:
        record.levelname_aligned = f"{record.levelname:<8}"

        for attr in ("run_id", "instrument", "signal_id", "cycle_id", "plan_id"):
            if not hasattr(record, attr):
                setattr(record, attr, "")

        parts = []
        if getattr(record, "run_id", ""):
            parts.append(f"run={record.run_id}")
        if getattr(record, "instrument", ""):
            parts.append(f"inst={record.instrument}")
        if getattr(record, "signal_id", ""):
            parts.append(f"sig={record.signal_id}")
        if getattr(record, "cycle_id", ""):
            parts.append(f"cyc={record.cycle_id}")
        if getattr(record, "plan_id", ""):
            parts.append(f"plan={record.plan_id}")

        record.corr = (" ".join(parts)) if parts else ""
        record.corr_bracket = (f" [{record.corr}]" if record.corr else "")

        return super().format(record)


class JournalingContextFilter(logging.Filter):
    """
    Inject correlation fields from JournalingContext into every LogRecord.

    Best-effort: if JournalingContext is unavailable or unbound, the filter
    is a transparent no-op.  The import path is resolved at runtime so this
    module can be used in packages that do not depend on trading_platform.
    """

    # Override point: set this class attribute to a callable that returns a
    # dict-like object (or None), allowing callers to plug in any context
    # provider without hard-coding an import path.
    context_provider = None  # type: ignore[assignment]

    def filter(self, record: logging.LogRecord) -> bool:
        provider = JournalingContextFilter.context_provider
        if provider is None:
            # Fall back to trading_platform's JournalingContext if available.
            try:
                from trading_platform.journaling.service import JournalingContext  # type: ignore
                provider = JournalingContext.get
            except Exception:
                return True

        try:
            ctx = provider() or {}
            if isinstance(ctx, dict):
                for k in ("run_id", "instrument", "signal_id", "cycle_id", "plan_id"):
                    v = ctx.get(k)
                    if v is not None:
                        setattr(record, k, str(v))
        except Exception:
            pass

        return True


def get_logger(class_or_name: Any = None) -> logging.Logger:
    """
    Return a UTF-8 console logger with the ``AlignedFormatter``.

    Usage::

        log = get_logger(self)          # uses self.__class__.__name__
        log = get_logger("MyModule")    # explicit name

    Format::

        2025-10-03 10:48:02 - INFO     - [BrokerConnection] [run=… inst=…] message
    """
    if not isinstance(class_or_name, str):
        class_or_name = getattr(class_or_name, "__class__", type("x", (), {})).__name__

    logger = logging.getLogger(class_or_name)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.addFilter(JournalingContextFilter())
        handler.setFormatter(
            AlignedFormatter(
                fmt="%(asctime)s - %(levelname_aligned)s - [%(name)s]%(corr_bracket)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler.setStream(sys.stdout)

        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    return logger

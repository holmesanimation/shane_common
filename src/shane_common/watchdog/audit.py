"""
Generic append-only audit log backed by a JSONL file.

Uses shane_common.events.jsonl.JsonlEventWriter internally.
Every except-Exception block calls report_exception from
trading_platform.utils.diag_logger if available, otherwise falls back to
traceback.print_exc() so no audit failure is swallowed silently.
"""
from __future__ import annotations

import traceback
from pathlib import Path

from ..events.jsonl import JsonlEventWriter


def _report(msg: str, exc: Exception) -> None:
    """Report an exception without a hard dependency on trading_platform."""
    try:
        from trading_platform.utils.diag_logger import report_exception  # type: ignore[import]
        report_exception(msg, kind="exception.watchdog.audit")
    except Exception:
        print(f"[watchdog.audit] {msg}")
        traceback.print_exc()


class AppendOnlyAuditLog:
    """
    Append-only JSONL audit log for watchdog events.

    Parameters
    ----------
    path:
        Absolute path to the ``.jsonl`` file.  The parent directory is
        created on first write if it does not exist.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._writer = JsonlEventWriter(
            path_factory=lambda: self._path,
            sanitize=True,
            ensure_ascii=False,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, record: dict) -> None:
        """Append *record* as one JSON line to the audit file."""
        try:
            self._writer.append(record)
        except Exception as exc:
            _report(
                f"AppendOnlyAuditLog.append failed (path={self._path})",
                exc,
            )

    def tail(self, n: int) -> list[dict]:
        """
        Return the last *n* records from the log as a list of dicts.

        Returns an empty list if the file does not exist or cannot be read.
        """
        import json

        if not self._path.exists():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
            records: list[dict] = []
            for line in lines[-n:]:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
            return records
        except Exception as exc:
            _report(f"AppendOnlyAuditLog.tail failed (path={self._path})", exc)
            return []

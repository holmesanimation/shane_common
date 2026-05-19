# shane_common/ui/log_viewer/log_row.py
"""
Pure LogRow dataclass — canonical schema for normalised log events.

Derived from trading_platform/gui/log_viewer/log_row.py (Phase 6).
No Qt, no trading-platform imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True, slots=True)
class LogRow:
    """
    Single normalised log event, ready for display in a log viewer.

    Fields
    ------
    ts : float
        Envelope ``ts`` parsed to epoch-seconds UTC.
    kind : str
        Verbatim ``envelope.kind``.
    severity : str
        One of ``TRACE``, ``DEBUG``, ``INFO``, ``WARN``, ``ERROR``.
    code : str
        Same as *kind* — stable, machine-readable identifier.
    instrument : str | None
        ``envelope.instrument``; ``None`` for app-wide events.
    message : str
        Human-readable summary (templated from payload by the normaliser).
    details : dict
        ``envelope.payload`` plus any correlation fields.
    importance : str
        ``HIGH``, ``MEDIUM``, or ``LOW``.
    problem : bool
        ``True`` if the event represents an error/warning condition.
    dedupe_count : int | None
        If hard-deduped, the number of suppressed repetitions.
    dedupe_last_ts : float | None
        If hard-deduped, the epoch-UTC timestamp of the last suppressed emission.
    """

    ts: float
    kind: str
    severity: str
    code: str
    instrument: Optional[str]
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    importance: str = "MEDIUM"
    problem: bool = False
    dedupe_count: Optional[int] = None
    dedupe_last_ts: Optional[float] = None
    wall_clock_ts: float = 0.0
    source_file: Optional[str] = None

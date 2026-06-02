from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class NotificationSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    URGENT = "URGENT"


@dataclass(frozen=True)
class NotificationEvent:
    """
    Deterministic, JSON-safe notification event.

    NOTE:
      - Keep payload small: do NOT include secrets, order sizes, account balances.
      - event_id is derived by NotifyManager (stable hashing).
    """
    ts: float
    severity: NotificationSeverity
    kind: str                 # e.g. "strategy.error", "capital.blocked_global"
    scope: str                # "instrument" | "strategy" | "global"
    strategy_id: Optional[str] = None
    instrument: Optional[str] = None
    details: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    dedupe_key: Optional[str] = None

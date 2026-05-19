"""
Generic watchdog liveness models.

Deliberately contains NO trading domain states, broker states, risk locks,
or execution mode concepts.  Trading apps extend these in their own packages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

HEARTBEAT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# AppLiveness enum
# ---------------------------------------------------------------------------

class AppLiveness(str, Enum):
    """
    Derived liveness state for a monitored application.

    The watchdog/supervisor assigns this value externally — monitored apps
    never write it themselves.
    """
    UNKNOWN          = "UNKNOWN"           # never seen a heartbeat file
    STARTING         = "STARTING"          # app_state == STARTING
    HEALTHY          = "HEALTHY"           # fresh heartbeat, app healthy
    STALE            = "STALE"             # heartbeat age exceeds stale threshold
    DEAD             = "DEAD"              # heartbeat age exceeds frozen threshold
    EXPECTED_EXIT    = "EXPECTED_EXIT"     # exit marker present and run_id matches
    UNEXPECTED_EXIT  = "UNEXPECTED_EXIT"   # stale beyond grace, no marker, pid gone


# ---------------------------------------------------------------------------
# LivenessHeartbeat dataclass
# ---------------------------------------------------------------------------

@dataclass
class LivenessHeartbeat:
    """
    Minimal generic heartbeat written by a monitored application.

    Fields
    ------
    schema_version : matches HEARTBEAT_SCHEMA_VERSION
    app_id         : unique identifier for this application instance
    pid            : OS process ID at the time of write
    ts_wall_utc    : wall-clock UTC timestamp (time.time())
    seq            : monotonically increasing sequence counter owned by the writer
    """
    schema_version: int
    app_id: str
    pid: int
    ts_wall_utc: float
    seq: int


# ---------------------------------------------------------------------------
# BaseLockState dataclass
# ---------------------------------------------------------------------------

@dataclass
class BaseLockState:
    """
    Minimal lock/safety state persisted by the watchdog state store.

    Downstream packages extend this with domain-specific lock flags.
    """
    locked: bool = False
    manual_intervention_required: bool = False
    reason: Optional[str] = None

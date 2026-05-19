"""Journal envelope construction for shane_common journaling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from shane_common.json_safety import sanitize_json
from shane_common.time import local_now_iso, utc_now_iso, utc_iso_from_epoch


@dataclass(frozen=True)
class JournalSource:
    """Source identity — no broker field; trading apps extend via their own profile."""
    app: str
    component: str


def build_journal_envelope(
    *,
    kind: str,
    run_id: str,
    source: JournalSource,
    payload: Any,
    app_clock_ts: Optional[float] = None,
    msg: Optional[str] = None,
    correlation: Optional[dict] = None,
    profile=None,
) -> dict:
    """
    Build a journal envelope dict.

    - local_TS is always the first key (wall-clock at emit time, never overrideable).
    - ts is UTC ISO-8601: uses app_clock_ts if provided, else wall-clock utc_now_iso().
    - payload is sanitized via sanitize_json.
    - Validates kind via profile.validate_kind() if a profile is provided.
    """
    if profile is not None:
        err = profile.validate_kind(kind)
        if err:
            raise ValueError(err)

    env = {
        "local_TS": local_now_iso(),
        "ts": utc_iso_from_epoch(app_clock_ts) if app_clock_ts is not None else utc_now_iso(),
        "run_id": run_id,
        "kind": kind,
        "source": {"app": source.app, "component": source.component},
        "payload": sanitize_json(payload),
    }
    if msg is not None:
        env["msg"] = msg
    if correlation is not None:
        env["correlation"] = correlation

    return env

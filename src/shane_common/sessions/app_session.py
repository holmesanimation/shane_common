"""App session identity — run ID and startup metadata."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from shane_common.time import utc_now_iso, local_now_iso


def new_run_id() -> str:
    """Return a new UUID v4 string."""
    return str(uuid.uuid4())


@dataclass(frozen=True)
class AppSession:
    run_id: str
    app_id: str
    app_name: str
    data_root: str
    system_root: str
    started_utc_iso: str
    started_local_iso: str
    pid: int


def make_app_session(
    app_id: str,
    app_name: str,
    data_root: str,
    system_root: str,
) -> AppSession:
    """Create an AppSession capturing the current process identity and startup time."""
    return AppSession(
        run_id=new_run_id(),
        app_id=app_id,
        app_name=app_name,
        data_root=str(data_root),
        system_root=str(system_root),
        started_utc_iso=utc_now_iso(),
        started_local_iso=local_now_iso(),
        pid=os.getpid(),
    )

"""
Generic ProcessLauncher — spawns a fully detached child process.

Provides rate-limiting (max restarts per hour), per-process cooldown, and
pre-launch callback hooks.  Contains no trading-specific imports.

Every except-Exception block uses _report() which calls report_exception()
if available, otherwise traceback.print_exc().
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_HOUR_S: float = 3600.0


def _report(msg: str) -> None:
    try:
        from trading_platform.utils.diag_logger import report_exception  # type: ignore[import]
        report_exception(msg, kind="exception.watchdog.process_launcher")
    except Exception:
        print(f"[watchdog.process_launcher] {msg}")
        traceback.print_exc()


@dataclass
class ProcessLaunchConfig:
    """
    Configuration for a single supervised process.

    Parameters
    ----------
    app_id:
        Unique identifier; used only for logging and rate-limit tracking.
    launch_cmd:
        The command line to spawn.  Passed directly to subprocess.Popen.
    max_restarts_per_hour:
        Rate limit.  0 disables restarts entirely.
    cooldown_s:
        Minimum seconds between consecutive restart attempts for this process.
    enabled:
        Master switch; if False, no restarts are attempted.
    """
    app_id: str
    launch_cmd: list[str]
    max_restarts_per_hour: int = 3
    cooldown_s: float = 30.0
    enabled: bool = True


@dataclass
class _RestartRecord:
    attempt_timestamps: list[float] = field(default_factory=list)
    last_attempt_ts: float = 0.0


class ProcessLauncher:
    """
    Manages safe restart of supervised processes.

    Parameters
    ----------
    pre_launch_hook:
        Optional callable called with *app_id* immediately before a spawn
        attempt.  Use it to write recovery flags, emit notifications, etc.
    post_launch_hook:
        Optional callable called with *app_id* after a successful Popen.
    """

    def __init__(
        self,
        pre_launch_hook: Optional[Callable[[str], None]] = None,
        post_launch_hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._pre_launch = pre_launch_hook
        self._post_launch = post_launch_hook
        self._records: dict[str, _RestartRecord] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def maybe_restart(self, cfg: ProcessLaunchConfig) -> bool:
        """
        Attempt to restart the process described by *cfg* if policy allows.

        Returns True when a spawn was attempted, False when gated.
        """
        if not cfg.enabled or not cfg.launch_cmd:
            return False

        record = self._records.setdefault(cfg.app_id, _RestartRecord())
        now = time.time()

        # Cooldown gate
        if now - record.last_attempt_ts < cfg.cooldown_s:
            log.debug(
                "ProcessLauncher: %s in cooldown (%.0f s remaining).",
                cfg.app_id,
                cfg.cooldown_s - (now - record.last_attempt_ts),
            )
            return False

        # Rate-limit gate
        cutoff = now - _HOUR_S
        recent = [t for t in record.attempt_timestamps if t >= cutoff]
        if len(recent) >= cfg.max_restarts_per_hour:
            log.warning(
                "ProcessLauncher: %s hit restart rate limit (%d/%d per hour).",
                cfg.app_id,
                len(recent),
                cfg.max_restarts_per_hour,
            )
            return False

        # Pre-launch hook
        if self._pre_launch is not None:
            try:
                self._pre_launch(cfg.app_id)
            except Exception:
                _report(
                    f"ProcessLauncher: pre_launch_hook failed for {cfg.app_id!r}"
                )

        # Spawn
        self._spawn(cfg)
        record.last_attempt_ts = now
        recent.append(now)
        record.attempt_timestamps = recent

        # Post-launch hook
        if self._post_launch is not None:
            try:
                self._post_launch(cfg.app_id)
            except Exception:
                _report(
                    f"ProcessLauncher: post_launch_hook failed for {cfg.app_id!r}"
                )

        return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _spawn(self, cfg: ProcessLaunchConfig) -> None:
        """Launch *cfg.launch_cmd* as a fully detached child process."""
        kwargs: dict = {
            "close_fds": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(cfg.launch_cmd, **kwargs)
            log.warning(
                "ProcessLauncher: spawned %r → PID %d.", cfg.app_id, proc.pid
            )
        except Exception:
            _report(
                f"ProcessLauncher: failed to spawn {cfg.app_id!r} "
                f"cmd={cfg.launch_cmd!r}"
            )

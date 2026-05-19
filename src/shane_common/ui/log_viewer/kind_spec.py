# shane_common/ui/log_viewer/kind_spec.py
"""
KindSpec dataclass and related helpers for log normalisation.

Extracted from trading_platform/gui/log_viewer/log_kind_map.py.
No Qt, no trading-platform imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True, slots=True)
class KindSpec:
    """Normalisation spec for a single journal kind."""

    severity: str
    message_template: str
    importance: str = "MEDIUM"
    problem: bool = False

    # Hard-dedupe interval in seconds, or None for no dedupe.
    dedupe_interval_s: Optional[float] = None

    # Callable (payload: dict) -> str | None that extracts a
    # discriminator value for the dedupe key.  None means use kind+instrument
    # only (no payload discrimination).
    dedupe_discriminator: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None

    # Callable (payload: dict) -> str for kinds with conditional severity.
    # When set, overrides *severity* at normalisation time.
    severity_fn: Optional[Callable[[Dict[str, Any]], str]] = None

    # Callable (payload: dict) -> bool for kinds with conditional problem flag.
    # When set, overrides *problem* at normalisation time.
    problem_fn: Optional[Callable[[Dict[str, Any]], bool]] = None

    # Callable (payload: dict) -> str for kinds with conditional message.
    # When set, overrides *message_template* at normalisation time.
    message_fn: Optional[Callable[[Dict[str, Any]], str]] = None

    # When True, suppress consecutive identical messages for the same
    # (kind, instrument) key — emit only when the message text changes.
    dedupe_until_changed: bool = False


# Type alias for a function that resolves a kind string to an optional KindSpec.
KindSpecResolver = Callable[[str], Optional[KindSpec]]

# Fallback used when no matching KindSpec is found.
_DEFAULT_KIND_SPEC = KindSpec(
    severity="DEBUG",
    message_template="{kind}",
    importance="LOW",
)


# ------------------------------------------------------------------ #
# Discriminator helpers (shared normalisation utilities)
# ------------------------------------------------------------------ #

def _disc_eligible(p: Dict[str, Any]) -> Optional[str]:
    return str(p.get("eligible", ""))


def _disc_channels_action(p: Dict[str, Any]) -> Optional[str]:
    return f"{p.get('channels', '')}|{p.get('action', '')}"


def _disc_channels(p: Dict[str, Any]) -> Optional[str]:
    return str(p.get("channels", ""))


def _disc_reason(p: Dict[str, Any]) -> Optional[str]:
    return str(p.get("reason", ""))


def _disc_state(p: Dict[str, Any]) -> Optional[str]:
    return str(p.get("state", ""))


def _disc_action(p: Dict[str, Any]) -> Optional[str]:
    return str(p.get("action", ""))


def _disc_gate(p: Dict[str, Any]) -> Optional[str]:
    return str(p.get("gate", ""))


# ------------------------------------------------------------------ #
# Template formatting helper
# ------------------------------------------------------------------ #

def _format_template(
    template: str,
    payload: Dict[str, Any],
    envelope: Dict[str, Any],
) -> str:
    """
    Interpolate ``{field}`` placeholders against *payload* (priority)
    then *envelope*, with safe fallback to ``"?"`` for missing keys.
    """
    import traceback

    ns: Dict[str, Any] = {}
    for k, v in envelope.items():
        if k != "payload":
            ns[k] = v
    ns.update(payload)

    try:
        return template.format_map(_SafeDict(ns))
    except Exception:
        traceback.print_exc()
        return template


class _SafeDict(dict):
    """Dict subclass that returns ``'?'`` for missing keys in str.format_map."""

    def __missing__(self, key: str) -> str:
        return "?"

"""Journal profile contract — taxonomy, stream routing, and path routing."""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Optional


class JournalProfile(abc.ABC):
    @abc.abstractmethod
    def validate_kind(self, kind: str) -> Optional[str]:
        """Return an error string if kind is invalid, None if OK."""
        ...

    @abc.abstractmethod
    def stream_for_kind(self, kind: str) -> str:
        """Return the JSONL stream name for this kind."""
        ...

    @abc.abstractmethod
    def route_event(self, envelope: dict) -> Path:
        """Return the JSONL output path for this envelope."""
        ...


class DefaultJournalProfile(JournalProfile):
    """A permissive pass-through profile for development."""

    def validate_kind(self, kind: str) -> Optional[str]:
        return None

    def stream_for_kind(self, kind: str) -> str:
        return kind.split(".")[0] if "." in kind else "misc"

    def route_event(self, envelope: dict) -> Path:
        raise NotImplementedError(
            "DefaultJournalProfile.route_event must be subclassed with a real routing strategy"
        )

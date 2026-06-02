from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from shane_common.notify.contracts import NotificationEvent


class INotifyAdapter(Protocol):
    def send(self, event: NotificationEvent) -> None:
        """Best-effort delivery. Must never raise."""
        ...

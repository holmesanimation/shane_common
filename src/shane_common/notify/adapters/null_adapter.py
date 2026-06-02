from __future__ import annotations

from shane_common.notify.contracts import NotificationEvent


class NullNotifyAdapter:
    """
    No-op adapter (safe default).
    Useful when running locally without paging configured.
    """
    def send(self, event: NotificationEvent) -> None:
        return

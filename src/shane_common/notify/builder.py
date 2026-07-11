from __future__ import annotations

from typing import Callable, Optional, Sequence

from shane_common.notify.adapters.base import INotifyAdapter
from shane_common.notify.adapters.null_adapter import NullNotifyAdapter
from shane_common.notify.adapters.telegram_adapter import TelegramNotifyAdapter
from shane_common.notify.contracts import NotificationEvent


def build_telegram_adapter(
    token: str,
    chat_ids: Sequence[int],
    alert_severities: frozenset[str],
    *,
    format_message: Optional[Callable[[NotificationEvent], str]] = None,
    build_reply_markup: Optional[Callable[[NotificationEvent], Optional[dict]]] = None,
    timeout_s: float = 3.0,
) -> INotifyAdapter:
    """
    Returns a ``TelegramNotifyAdapter`` when *token* and *chat_ids* are both
    non-empty, otherwise a ``NullNotifyAdapter``.

    Parameters
    ----------
    token:
        Telegram bot token.  Typically read from an environment variable by
        the caller; an empty string produces a ``NullNotifyAdapter``.
    chat_ids:
        Sequence of integer Telegram chat IDs to deliver to.  An empty
        sequence produces a ``NullNotifyAdapter``.
    alert_severities:
        Frozenset of severity value strings to deliver.
        Example: ``frozenset({"WARNING", "URGENT"})``
    format_message:
        Optional ``(NotificationEvent) -> str`` formatter.  Defaults to the
        built-in plain-text summary when ``None``.
    build_reply_markup:
        Optional ``(NotificationEvent) -> dict | None`` keyboard builder.
    timeout_s:
        HTTP request timeout in seconds (default 3.0).
    """
    if not token or not chat_ids:
        return NullNotifyAdapter()
    return TelegramNotifyAdapter(
        bot_token=token,
        recipient_ids=list(chat_ids),
        alert_severities=alert_severities,
        format_message=format_message,
        build_reply_markup=build_reply_markup,
        timeout_s=timeout_s,
    )

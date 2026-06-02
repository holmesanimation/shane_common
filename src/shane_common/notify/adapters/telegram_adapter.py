from __future__ import annotations

import json
import logging
import traceback
import urllib.parse
import urllib.request
from typing import Callable, Optional, Sequence

from shane_common.notify.contracts import NotificationEvent

log = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"


def _default_format_message(event: NotificationEvent) -> str:
    """Plain-text fallback renderer used when no formatter is injected."""
    parts = [f"[{event.severity.value}] {event.kind}"]
    if event.scope:
        parts.append(f"scope={event.scope}")
    if event.strategy_id:
        parts.append(f"strategy={event.strategy_id}")
    if event.instrument:
        parts.append(f"instrument={event.instrument}")
    if event.details:
        parts.append(str(event.details)[:500])
    return "\n".join(parts)


class TelegramNotifyAdapter:
    """
    Generic Telegram push notification adapter.

    Transport-only: HTTP delivery via stdlib ``urllib.request``.
    No dependency on any domain-specific formatter.

    Parameters
    ----------
    bot_token:
        Telegram bot token from BotFather.
    recipient_ids:
        Sequence of Telegram chat IDs to send to.
    alert_severities:
        Frozenset of severity value strings that should be delivered.
        Events whose severity is not in this set are silently skipped.
        Example: ``frozenset({"WARNING", "URGENT"})``
    format_message:
        Optional callable ``(NotificationEvent) -> str`` that renders the
        text body to send.  Defaults to a plain-text summary when None.
    build_reply_markup:
        Optional callable ``(NotificationEvent) -> dict | None`` that
        returns a JSON-ready ``reply_markup`` dict (e.g. an inline keyboard)
        or ``None`` when no keyboard is needed.  Ignored when None.
    timeout_s:
        HTTP request timeout in seconds.
    """

    def __init__(
        self,
        bot_token: str,
        recipient_ids: Sequence[int],
        alert_severities: frozenset[str],
        format_message: Callable[[NotificationEvent], str] | None = None,
        build_reply_markup: Callable[[NotificationEvent], dict | None] | None = None,
        timeout_s: float = 3.0,
    ) -> None:
        self._token = bot_token
        self._recipient_ids = list(recipient_ids)
        self._alert_severities = alert_severities
        self._format_message = format_message or _default_format_message
        self._build_reply_markup = build_reply_markup
        self._timeout_s = float(timeout_s)

    def send(self, event: NotificationEvent) -> None:
        try:
            if event.severity.value not in self._alert_severities:
                return

            text = self._format_message(event)

            reply_markup_json: str | None = None
            if self._build_reply_markup is not None:
                try:
                    markup = self._build_reply_markup(event)
                    if markup is not None:
                        reply_markup_json = json.dumps(markup)
                except Exception:
                    log.warning(
                        "TelegramNotifyAdapter: build_reply_markup raised; "
                        "sending without keyboard",
                        exc_info=True,
                    )

            for chat_id in self._recipient_ids:
                self._post(chat_id=chat_id, text=text, reply_markup=reply_markup_json)

        except Exception:
            # MUST NOT raise.
            log.exception(
                "TelegramNotifyAdapter.send: unexpected error for kind=%r",
                getattr(event, "kind", "?"),
            )

    def _post(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: str | None,
    ) -> None:
        url = f"{_TELEGRAM_API_BASE}/bot{self._token}/sendMessage"
        params: dict[str, str] = {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup

        encoded = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            data=encoded,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                _ = resp.read()
        except Exception as exc:
            log.warning(
                "TelegramNotifyAdapter: POST to chat_id=%d failed: %s",
                chat_id,
                exc,
            )

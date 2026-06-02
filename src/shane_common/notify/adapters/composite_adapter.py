from __future__ import annotations

import logging
import traceback
from typing import Sequence

from shane_common.notify.contracts import NotificationEvent

log = logging.getLogger(__name__)


class CompositeNotifyAdapter:
    """
    Fan-out adapter: delivers to all child adapters in order.

    Each child is called in a try/except so that one failing adapter
    never prevents delivery to the remaining adapters.
    """

    def __init__(self, adapters: Sequence) -> None:
        self._adapters = list(adapters)

    def send(self, event: NotificationEvent) -> None:
        for adapter in self._adapters:
            try:
                adapter.send(event)
            except Exception:
                # Log and continue; never raise.
                log.exception(
                    "CompositeNotifyAdapter: adapter %s raised unexpectedly",
                    type(adapter).__name__,
                )

from __future__ import annotations

import json
import logging
import math
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from shane_common.notify.contracts import NotificationEvent, NotificationSeverity

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BarkConfig:
    device_key: str
    base_url: str = "https://api.day.app"
    timeout_s: float = 2.0

    # Optional Bark niceties
    group: str = "app"
    sound: Optional[str] = "alarm"


class BarkNotifyAdapter:
    """
    Best-effort Bark push notification adapter (https://bark.day.app).

    Security model:
      - device_key is a bearer token; do NOT include secrets in details/data.
      - Uses stdlib urllib only; no third-party HTTP dependency.
    """

    def __init__(self, cfg: BarkConfig) -> None:
        self._cfg = cfg

    def send(self, event: NotificationEvent) -> None:
        try:
            title, body = self._format(event)

            # Bark endpoint: /<device_key>/<message>
            msg = urllib.parse.quote(body, safe="")
            url = f"{self._cfg.base_url.rstrip('/')}/{self._cfg.device_key}/{msg}"

            params: dict[str, str] = {"title": title, "group": self._cfg.group}
            if self._cfg.sound and event.severity in (
                NotificationSeverity.WARNING,
                NotificationSeverity.URGENT,
            ):
                params["sound"] = self._cfg.sound

            url = f"{url}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url=url, method="GET")
            with urllib.request.urlopen(req, timeout=float(self._cfg.timeout_s)) as resp:
                # Read to complete the request; ignore body.
                _ = resp.read()
        except Exception:
            # MUST NOT raise.
            return

    def _format(self, event: NotificationEvent) -> tuple[str, str]:
        sev = str(event.severity.value)
        title = f"[{sev}] {event.kind}"

        parts: list[str] = []
        if event.strategy_id:
            parts.append(f"strategy={event.strategy_id}")
        if event.instrument:
            parts.append(f"instrument={event.instrument}")
        parts.append(f"scope={event.scope}")
        parts.append(f"ts={event.ts:.3f}")
        if event.details:
            parts.append(f"details={event.details}")
        if event.data:
            parts.append("data=" + _safe_compact_json(event.data))

        body = "\n".join(parts)
        return title, body


def _safe_compact_json(obj: object) -> str:
    def sanitize(x: object) -> object:
        if isinstance(x, float):
            if math.isnan(x) or math.isinf(x):
                return None
            return float(x)
        if isinstance(x, dict):
            return {str(k): sanitize(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [sanitize(v) for v in x]
        return x

    try:
        clean = sanitize(obj)
        return json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except Exception:
        traceback.print_exc()
        return '"<unserializable>"'

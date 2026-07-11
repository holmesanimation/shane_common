# Notification System

`shane_common.notify` provides a domain-neutral push notification layer. Apps build a
`NotifyManager` with one or more transport adapters and call it when something notable
happens. Anti-spam windowing and hard idempotency are owned by the manager; transport
details are entirely inside the adapter.

`trading_platform` uses Bark (iOS push) and Telegram. `purity_app` can wire in any
adapter it chooses. Supervisor-specific formatting (emoji, inline keyboards) never
enters `shane_common` — callers inject formatters as callables.

---

## Architecture overview

```
App startup
  └─ NotifyManager(adapter=..., clock_fn=..., enabled=True)
       │
       ├─ idempotency gate    ← stable SHA-256 event_id; same event never re-delivered
       ├─ anti-spam window    ← per-key minimum interval per severity
       └─ INotifyAdapter
            ├─ NullNotifyAdapter      ← simulation / test (no-op)
            ├─ BarkNotifyAdapter      ← iOS push via api.day.app
            ├─ TelegramNotifyAdapter  ← Telegram bot, injectable formatter
            └─ CompositeNotifyAdapter ← fan-out to multiple adapters
```

---

## Packages

| Package | Purpose |
|---|---|
| `shane_common.notify.contracts` | `NotificationSeverity`, `NotificationEvent` |
| `shane_common.notify.notify_manager` | `NotifyManager` |
| `shane_common.notify.adapters.base` | `INotifyAdapter` Protocol |
| `shane_common.notify.adapters.null_adapter` | `NullNotifyAdapter` |
| `shane_common.notify.adapters.bark_adapter` | `BarkNotifyAdapter`, `BarkConfig` |
| `shane_common.notify.adapters.telegram_adapter` | `TelegramNotifyAdapter` |
| `shane_common.notify.adapters.composite_adapter` | `CompositeNotifyAdapter` |
| `shane_common.notify.builder` | `build_telegram_adapter` convenience factory |

---

## Step 1 — Choose an adapter

### Null (simulation / test)

```python
from shane_common.notify.adapters.null_adapter import NullNotifyAdapter

adapter = NullNotifyAdapter()
```

### Bark (iOS push)

Bark sends push notifications to iOS via [api.day.app](https://bark.day.app).
Set `BARK_API_KEY` in the environment (never hard-code the device key).

```python
import os
from shane_common.notify.adapters.bark_adapter import BarkConfig, BarkNotifyAdapter

adapter = BarkNotifyAdapter(
    BarkConfig(
        device_key=os.environ["BARK_API_KEY"],
        base_url=os.environ.get("BARK_BASE_URL", "https://api.day.app"),
        timeout_s=2.0,
        group="my_app",   # groups notifications in the Bark app
        sound="alarm",    # played on WARNING and URGENT only
    )
)
```

### Telegram

Requires a bot token from BotFather (store in an environment variable) and at least
one chat ID. By default only WARNING and URGENT events are delivered; pass any
`frozenset` of severity strings to change the filter.

```python
import os
from shane_common.notify.adapters.telegram_adapter import TelegramNotifyAdapter

adapter = TelegramNotifyAdapter(
    bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
    recipient_ids=[123456789],
    alert_severities=frozenset({"WARNING", "URGENT"}),
    # format_message=None  →  plain-text default
    # build_reply_markup=None  →  no inline keyboard
)
```

#### Telegram convenience factory (recommended)

When your app has already parsed token/chat IDs, prefer the shared builder.
It returns `NullNotifyAdapter` automatically when token or chat IDs are empty.

```python
from shane_common.notify.builder import build_telegram_adapter

adapter = build_telegram_adapter(
    token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
    chat_ids=[123456789],
    alert_severities=frozenset({"WARNING", "URGENT"}),
)
```

#### Injecting a custom formatter

The adapter is transport-only. Domain-specific formatting belongs in the caller:

```python
from shane_common.notify.contracts import NotificationEvent

def _my_format(event: NotificationEvent) -> str:
    return f"*{event.kind}* [{event.severity.value}]\n{event.details or ''}"

def _my_keyboard(event: NotificationEvent) -> dict | None:
    if event.severity.value == "URGENT":
        return {"inline_keyboard": [[{"text": "Acknowledge", "callback_data": "ack"}]]}
    return None

adapter = TelegramNotifyAdapter(
    bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
    recipient_ids=[123456789],
    alert_severities=frozenset({"WARNING", "URGENT"}),
    format_message=_my_format,
    build_reply_markup=_my_keyboard,
)
```

### Composite (fan-out)

Deliver to multiple adapters. If one raises, the others still receive the event.

```python
from shane_common.notify.adapters.composite_adapter import CompositeNotifyAdapter

adapter = CompositeNotifyAdapter([bark_adapter, telegram_adapter])
```

---

## Step 2 — Build a NotifyManager

```python
from shane_common.notify.notify_manager import NotifyManager

mgr = NotifyManager(
    adapter=adapter,
    clock_fn=None,                        # see below
    min_emit_interval_s_default=60.0,     # INFO window
    min_emit_interval_s_warning=120.0,    # WARNING window
    min_emit_interval_s_urgent=180.0,     # URGENT window
    enabled=True,
)
```

### `clock_fn` — deterministic time injection

`clock_fn` is an optional `() -> float` that returns the current time as epoch seconds.
When `None`, the manager falls back to `time.time()`.

Inject a clock when:
- **Replay mode**: you want notifications suppressed or to use replay time.
- **Deterministic testing**: fix the clock so anti-spam windows are predictable.
- **App-owned clock**: tie notifications to the same time source as everything else.

```python
# In a trading app that owns an authoritative clock:
mgr = NotifyManager(
    adapter=adapter,
    clock_fn=lambda: app.clock.now_ts if app.clock is not None else None,
)

# In tests — fix the clock at a known timestamp:
mgr = NotifyManager(adapter=adapter, clock_fn=lambda: 1_000_000.0)

# Disable notifications in simulation without None-checking at call sites:
mgr = NotifyManager(adapter=NullNotifyAdapter(), enabled=False)
```

---

## Step 3 — Emit notifications

All emit calls are **best-effort**: they never raise, and they return `None` when
suppressed or when `enabled=False`.

### High-level helpers (recommended)

```python
mgr.notify_info(
    kind="data.stale",
    scope="instrument",
    instrument="BTC/USD",
    details="No tick for 90 s",
    dedupe_key="data.stale:BTC/USD",
)

mgr.notify_warning(
    kind="capital.low",
    scope="global",
    details="Available capital below threshold",
    dedupe_key="capital.low:global",
)

mgr.notify_urgent(
    kind="process.dead",
    scope="global",
    strategy_id="kraken_breakout",
    details="Strategy process exited unexpectedly",
    dedupe_key="process.dead:kraken_breakout",
)
```

### Low-level emit (when you have a pre-built event)

```python
from shane_common.notify.contracts import NotificationEvent, NotificationSeverity

event = NotificationEvent(
    ts=1_000_000.0,
    severity=NotificationSeverity.URGENT,
    kind="risk.limit_breached",
    scope="global",
    details="Daily loss limit exceeded",
    dedupe_key="risk.limit_breached:global",
)
event_id = mgr.emit(event)  # str if delivered, None if suppressed
```

---

## Idempotency and anti-spam

### Hard idempotency

Each event is identified by a stable SHA-256 hash of:
`(kind, scope, severity, strategy_id, instrument, dedupe_key)`

`ts`, `details`, and `data` are **excluded** from the hash. An event with the same
logical identity is delivered at most once per process lifetime.

### Anti-spam windowing

The window key is the same tuple as the idempotency hash. Within the applicable
minimum interval for the event's severity, a second emit of the same key is suppressed.

| Severity | Default window |
|---|---|
| INFO | 60 s |
| WARNING | 120 s |
| URGENT | 180 s |

### `dedupe_key` guidance

Always supply an explicit `dedupe_key` when the same logical problem can recur over
multiple ticks or heartbeats:

```python
# Good — scoped to the specific instrument and problem
dedupe_key="marketdata.stale:BTC/USD"

# Good — scoped to the specific app transition
dedupe_key=f"supervisor:{app_id}:{prev_state}->{new_state}"

# Avoid — omitting dedupe_key means every unique (kind, scope, severity)
# can only be emitted once per process
```

---

## Wiring pattern

The recommended pattern is a module-level factory function that reads credentials from
the environment and falls back to `NullNotifyAdapter` on any failure.

```python
import os
import logging
from shane_common.notify.notify_manager import NotifyManager
from shane_common.notify.adapters.bark_adapter import BarkConfig, BarkNotifyAdapter
from shane_common.notify.adapters.null_adapter import NullNotifyAdapter

log = logging.getLogger(__name__)

def build_notify_manager(*, enabled: bool = True) -> NotifyManager:
    try:
        device_key = os.environ.get("BARK_API_KEY", "").strip()
        if device_key:
            adapter = BarkNotifyAdapter(
                BarkConfig(device_key=device_key, group="my_app")
            )
            log.info("Notifications: Bark enabled.")
        else:
            adapter = NullNotifyAdapter()
            log.info("Notifications: BARK_API_KEY not set — using null adapter.")

        return NotifyManager(adapter=adapter, enabled=enabled)
    except Exception:
        log.exception("build_notify_manager failed; falling back to null adapter.")
        return NotifyManager(adapter=NullNotifyAdapter(), enabled=False)
```

Disable entirely in simulation:

```python
notify_manager = build_notify_manager(enabled=False)
```

---

## Writing a custom adapter

Implement `send(event: NotificationEvent) -> None`. The method **must never raise**:

```python
from shane_common.notify.contracts import NotificationEvent

class SlackNotifyAdapter:
    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    def send(self, event: NotificationEvent) -> None:
        try:
            import urllib.request, json
            payload = json.dumps({"text": f"[{event.severity.value}] {event.kind}"})
            req = urllib.request.Request(
                self._url,
                data=payload.encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3.0):
                pass
        except Exception:
            pass  # best-effort; never raise
```

---

## Security notes

- Do **not** put secrets, account balances, order sizes, or PII in `details` or `data`.
  These fields are delivered verbatim to external services (Bark, Telegram).
- `device_key` and `bot_token` are bearer tokens. Load them from environment variables
  or a secrets store; never hard-code them or log them.
- `CompositeNotifyAdapter` isolates failures between children but does not retry.
  A failed delivery is logged at WARNING level and discarded.

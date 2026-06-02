# Logging & Console Capture

## Overview

`shane_common` owns the canonical implementations of two logging utilities:

| Module | Class / function | Purpose |
|---|---|---|
| `shane_common.logging.logging_utils` | `get_logger`, `AlignedFormatter`, `JournalingContextFilter` | Structured console logger with optional correlation fields |
| `shane_common.logging.console_capture` | `ConsoleCapture` | Tee `sys.stdout`/`sys.stderr` to a rolling CSV file |

`trading_platform` re-exports both from its legacy paths so **no existing import statement needs to change**:

```python
# These continue to work unchanged:
from trading_platform.utils.logging_utils import get_logger
from trading_platform.utils.console_capture import ConsoleCapture
```

---

## get_logger

```python
from shane_common.logging.logging_utils import get_logger

log = get_logger(self)           # name = self.__class__.__name__
log = get_logger("MyModule")     # explicit name
log.info("Connection established")
```

**Output format**

```
2026-05-19 10:48:02 - INFO     - [BrokerConnection] message
2026-05-19 10:48:03 - INFO     - [BrokerConnection] [run=abc inst=AAPL] message
```

Correlation fields (`run=`, `inst=`, `sig=`, `cyc=`, `plan=`) appear only when a
`JournalingContext` is active.  If no context is bound the brackets are omitted
entirely.

### AlignedFormatter

`AlignedFormatter` is a `logging.Formatter` subclass that:
- Left-pads `levelname` to 8 characters.
- Injects `run_id`, `instrument`, `signal_id`, `cycle_id`, `plan_id` attributes
  onto every `LogRecord` (empty string when not provided), so format strings that
  reference them never raise `KeyError`.
- Builds a compact `corr_bracket` string shown after the logger name.

### JournalingContextFilter

Attached to every handler created by `get_logger`.  On each log call it
attempts to read the current `JournalingContext` and stamps the matching
attributes on the record.

**Plugging in a custom context provider** (useful in packages that do not depend
on `trading_platform`):

```python
from shane_common.logging.logging_utils import JournalingContextFilter

def my_context() -> dict:
    return {"run_id": "run-001", "instrument": "BTC/USD"}

JournalingContextFilter.context_provider = my_context
```

Set `context_provider` once at startup; it is used by all subsequent log calls.
When `context_provider` is `None` the filter falls back to
`trading_platform.journaling.service.JournalingContext.get` if that import
resolves, and is a transparent no-op otherwise.

---

## ConsoleCapture

Intercepts every `write()` to `sys.stdout` and `sys.stderr`, passes the text
through to the original stream unchanged, and appends each line as a CSV row.

### CSV columns

| Column | Content |
|---|---|
| `Time` | ISO-8601 UTC timestamp |
| `Level` | Severity parsed from text (`TRACE`/`DEBUG`/`INFO`/`WARN`/`ERROR`); stderr lines default to `ERROR` |
| `Message` | Full captured line |

### Lifecycle

```python
from shane_common.logging.console_capture import ConsoleCapture

# 1. Start at process entry (idempotent — safe to call multiple times).
ConsoleCapture.start()

# 2. Optionally supply a custom directory (default: <TEMP>/shane_common/logs/).
ConsoleCapture.start(log_dir="/var/log/myapp")

# 3. Once the app knows its run_id, relocate the early temp file.
ConsoleCapture.relocate(
    new_dir=os.path.join(logs_root, app_name, run_id),
    filename=f"console_{run_id}",   # .csv appended automatically
)

# 4. Teardown (optional — file is flushed/closed on process exit).
ConsoleCapture.stop()
```

`start()` prunes `console_*.csv` files older than 7 days before creating
a new file.

### How to hook it up in a new app

1. Import and call `start()` as early as possible — ideally in the package's
   `__init__.py` or at the very top of `__main__`:

   ```python
   # myapp/__init__.py
   from shane_common.logging.console_capture import ConsoleCapture
   ConsoleCapture.start()
   ```

2. After your app session is initialised (so `logs_root` and `run_id` are
   known), call `relocate()` to move the temp CSV beside your other log files.

3. The app does not need to call `stop()` — the CSV is flushed to disk on
   every line and closed normally when the process exits.

---

## How trading_platform hooks these up

`trading_platform/__init__.py` starts the capture immediately:

```python
from trading_platform.utils.console_capture import ConsoleCapture
ConsoleCapture.start()
```

`trading_platform/app.py` (inside `TradingApp.__init__`) calls `relocate()`
once the session is ready:

```python
from trading_platform.utils.console_capture import ConsoleCapture
ConsoleCapture.relocate(
    os.path.join(self.logs_root, self.__class__.__name__, self.run_id),
    filename=f"console_{self.run_id}",
)
```

The re-export shims in `trading_platform/utils/` mean that every file that
uses the legacy import path continues to resolve to the same object in
`shane_common` — no migration is needed.

---

## Implementing in a new package

```
mypackage/
    __init__.py          ← ConsoleCapture.start() here
    logging_shim.py      ← optional: re-export get_logger under a local alias
```

**Minimal `__init__.py`**

```python
from shane_common.logging.console_capture import ConsoleCapture
from shane_common.logging.logging_utils import get_logger          # noqa: F401

ConsoleCapture.start()
```

**Optional re-export shim** (keeps existing import paths intact if migrating
from a local copy):

```python
# mypackage/utils/logging_utils.py
from shane_common.logging.logging_utils import (  # noqa: F401
    AlignedFormatter,
    JournalingContextFilter,
    get_logger,
)
__all__ = ["AlignedFormatter", "JournalingContextFilter", "get_logger"]

# mypackage/utils/console_capture.py
from shane_common.logging.console_capture import ConsoleCapture  # noqa: F401
__all__ = ["ConsoleCapture"]
```

---

## File locations

| File | Role |
|---|---|
| `shane_common/src/shane_common/logging/logging_utils.py` | Canonical implementation |
| `shane_common/src/shane_common/logging/console_capture.py` | Canonical implementation |
| `trading_platform/utils/logging_utils.py` | Re-export shim (backward compat) |
| `trading_platform/utils/console_capture.py` | Re-export shim (backward compat) |

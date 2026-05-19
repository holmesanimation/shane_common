# Log Viewer

`shane_common` provides a generic, Qt-based log viewer layer that sits **downstream of the
journaling system**. It converts structured journal envelopes into normalised display rows
and feeds them to a `QTableView`. Apps supply only their kind taxonomy; all normalisation,
deduplication, filtering, and timezone display logic lives in the shared layer.

See [`journaling_system.md`](journaling_system.md) for the upstream pipeline (profiles,
services, and JSONL sinks).

---

## Architecture overview

```
JournalService
  ├─ JsonlJournalSink             ← disk (existing)
  └─ BaseLogNormalizerSink        ← NEW: normalise envelopes → LogRow
       │
       KindSpecResolver           ← app-supplied: kind str → KindSpec
       │
       LogRowEmitter
            │
            log_row_appended (Qt Signal)
            │
            ▼
       LogViewerWindow.append_row
            │
            ▼
       LogTableModel (list[LogRow])    ← QAbstractTableModel
            │
            ▼
       LogFilterProxyModel             ← severity / type / instrument filter
            │
            ▼
       QTableView
```

The normaliser sink is just another entry in `JournalService._sinks`. Every call to
`journal.emit(...)` fans out to both the disk sink and the normaliser sink simultaneously.

---

## Packages

| Package | Purpose |
|---|---|
| `shane_common.ui.log_viewer.log_row` | `LogRow` dataclass — canonical display schema |
| `shane_common.ui.log_viewer.kind_spec` | `KindSpec`, `KindSpecResolver`, `_format_template` |
| `shane_common.ui.log_viewer.base_normalizer_sink` | `BaseLogNormalizerSink` — abstract sink with full normalisation + dedupe logic |
| `shane_common.ui.log_viewer.log_row_emitter` | `LogRowEmitter` (Qt Signal), `CallbackLogRowEmitter` (no Qt) |
| `shane_common.ui.log_viewer.log_table_model` | `LogTableModel`, `LogFilterProxyModel` |

---

## Step 1 — Define KindSpecs

Create one `KindSpec` per journal kind your app emits. Each spec describes how to
normalise that kind into a display row.

```python
# myapp/services/log_kind_map.py
from typing import Optional
from shane_common.ui.log_viewer.kind_spec import KindSpec, _DEFAULT_KIND_SPEC

_KIND_MAP: dict[str, KindSpec] = {
    "system.start": KindSpec(
        severity="INFO",
        message_template="App started",
        importance="MEDIUM",
    ),
    "system.stop": KindSpec(
        severity="INFO",
        message_template="App stopped: {reason}",
        importance="MEDIUM",
    ),
    "system.alive": KindSpec(
        severity="DEBUG",
        message_template="Alive tick",
        importance="LOW",
        dedupe_interval_s=86400.0,   # suppress repeated ticks within 24 h
    ),
    "task.failed": KindSpec(
        severity="ERROR",
        message_template="Task failed: {task_id} — {reason}",
        importance="HIGH",
        problem=True,
    ),
}

def spec_for_kind(kind: str) -> Optional[KindSpec]:
    return _KIND_MAP.get(kind, _DEFAULT_KIND_SPEC)
```

**Message templates** use `str.format_map` against the merged `payload` + envelope dict.
Any missing key is rendered as `"?"`. Access nested envelope fields (e.g. `{run_id}`,
`{kind}`) directly alongside payload fields.

---

## Step 2 — Create a normalizer sink

Subclass `BaseLogNormalizerSink`. Provide a `kind_spec_fn` and an `emitter`. That's all.

```python
# myapp/gui/log_normalizer_sink.py
from shane_common.ui.log_viewer.base_normalizer_sink import BaseLogNormalizerSink
from shane_common.ui.log_viewer.log_row_emitter import LogRowEmitter, LogRowEmitterProtocol
from myapp.services.log_kind_map import spec_for_kind


class MyLogNormalizerSink(BaseLogNormalizerSink):

    def __init__(self) -> None:
        super().__init__(kind_spec_fn=spec_for_kind, emitter=LogRowEmitter())

    @property
    def emitter(self) -> LogRowEmitterProtocol:
        return self._emitter
```

`LogRowEmitter` wraps a PySide6 `Signal(object)` and maintains a 50 000-row ring buffer
so windows opened after startup can be seeded with historical rows (see Step 4).

---

## Step 3 — Register the sink with JournalService

Add the sink to `journal._sinks` before any events are emitted. The journaling service
fans out to all sinks on every `journal.emit(...)` call.

```python
# myapp/app.py (startup)
self._log_sink = MyLogNormalizerSink()
runtime.journal._sinks.append(self._log_sink)
```

From this point every `journal.emit(kind, payload, ...)` call automatically:
1. Writes a JSONL line to disk (via `JsonlJournalSink`)
2. Normalises the envelope to a `LogRow` and fires `emitter.log_row_appended`

---

## Step 4 — Connect to a log viewer window

Build the window lazily (on first open) and connect the signal.

```python
# myapp/app.py (menu / tray action)
def _open_log_viewer(self):
    if self._log_viewer_win is None:
        self._log_viewer_win = MyLogViewerWindow(
            sink=self._log_sink, parent=None
        )
        self._log_sink.emitter.log_row_appended.connect(
            self._log_viewer_win.append_row
        )
    self._log_viewer_win.show()
    self._log_viewer_win.raise_()
    self._log_viewer_win.activateWindow()
```

---

## Step 5 — Build the log viewer window

`LogTableModel` and `LogFilterProxyModel` from `shane_common` are the only Qt objects
needed. Wire them to a `QTableView` and expose `append_row`.

```python
# myapp/gui/log_viewer_window.py
from PySide6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QTableView
from PySide6.QtWidgets import QHeaderView
from shane_common.ui.log_viewer.log_table_model import (
    LogTableModel, LogFilterProxyModel, COL_MESSAGE, LOG_ROW_ROLE,
)
from myapp.gui.log_normalizer_sink import MyLogNormalizerSink


class MyLogViewerWindow(QMainWindow):

    def __init__(self, sink: MyLogNormalizerSink, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("My App — Log Viewer")
        self.resize(1100, 600)

        self._model = LogTableModel()
        self._proxy = LogFilterProxyModel()
        self._proxy.setSourceModel(self._model)

        view = QTableView()
        view.setModel(self._proxy)
        view.horizontalHeader().setSectionResizeMode(
            COL_MESSAGE, QHeaderView.ResizeMode.Stretch
        )

        root = QWidget()
        QVBoxLayout(root).addWidget(view)
        self.setCentralWidget(root)

        # Seed buffered rows emitted before this window was created.
        for row in sink.emitter.drain_buffer():
            self._model.append_row(row)

    def append_row(self, row) -> None:
        """Slot — called by emitter.log_row_appended signal."""
        self._model.append_row(row)
```

---

## LogRow schema

Every normalised event is an immutable `LogRow` dataclass:

| Field | Type | Source |
|---|---|---|
| `ts` | `float` | `envelope["ts"]` parsed to epoch-seconds UTC |
| `kind` | `str` | `envelope["kind"]` verbatim |
| `severity` | `str` | `KindSpec.severity` (or `severity_fn` result) |
| `code` | `str` | Same as `kind` — stable machine ID |
| `instrument` | `str \| None` | `envelope["instrument"]`; `None` for app-wide events |
| `message` | `str` | `KindSpec.message_template` formatted against payload (or `message_fn` result) |
| `details` | `dict` | `payload` merged with `correlation` fields |
| `importance` | `str` | `"HIGH"`, `"MEDIUM"`, or `"LOW"` |
| `problem` | `bool` | `True` if the event represents an error/warning condition |
| `dedupe_count` | `int \| None` | Count of suppressed repetitions (hard-dedupe) |
| `dedupe_last_ts` | `float \| None` | Epoch-UTC of last suppressed emission |
| `wall_clock_ts` | `float` | `time.time()` captured at normalisation, not app-clock |
| `source_file` | `str \| None` | `envelope["_source_file"]` if present |

---

## KindSpec fields reference

| Field | Type | Default | Purpose |
|---|---|---|---|
| `severity` | `str` | — | `TRACE`, `DEBUG`, `INFO`, `WARN`, or `ERROR` |
| `message_template` | `str` | — | `str.format_map` template; `{field}` resolves against payload then envelope |
| `importance` | `str` | `"MEDIUM"` | `"HIGH"`, `"MEDIUM"`, or `"LOW"` |
| `problem` | `bool` | `False` | Marks the row as an error/warning for UI highlighting |
| `dedupe_interval_s` | `float \| None` | `None` | Suppress repeats within this window (seconds) |
| `dedupe_discriminator` | `callable \| None` | `None` | `(payload) -> str` — include payload field in the dedupe key |
| `dedupe_until_changed` | `bool` | `False` | Suppress consecutive identical messages for the same `(kind, instrument)` |
| `severity_fn` | `callable \| None` | `None` | `(payload) -> str` — overrides `severity` at normalisation time |
| `problem_fn` | `callable \| None` | `None` | `(payload) -> bool` — overrides `problem` at normalisation time |
| `message_fn` | `callable \| None` | `None` | `(payload) -> str` — overrides `message_template` at normalisation time |

---

## Deduplication

Two deduplication strategies are available per kind:

**Hard dedupe** (`dedupe_interval_s`): suppresses any emission of the same `(kind,
instrument, discriminator)` key within a rolling time window. The first emission in a new
window is let through. Subsequent emissions within the window are discarded; the count and
last timestamp are attached to the next unblocked row via `dedupe_count` / `dedupe_last_ts`.

```python
# Suppress repeated alive ticks; let one through per 24-hour window.
"system.alive": KindSpec(
    severity="DEBUG",
    message_template="Alive",
    dedupe_interval_s=86400.0,
)

# Discriminate by payload field so different error codes dedupe independently.
"order.rejected": KindSpec(
    severity="WARN",
    message_template="Order rejected: {reason}",
    dedupe_interval_s=5.0,
    dedupe_discriminator=lambda p: str(p.get("reason", "")),
)
```

**Until-changed dedupe** (`dedupe_until_changed=True`): suppresses consecutive emissions
of the same `(kind, instrument)` pair that produce an identical message string. Useful for
status-transition kinds where you only want to see actual changes.

```python
"connection.status": KindSpec(
    severity="INFO",
    message_template="Connection: {state}",
    dedupe_until_changed=True,
)
```

---

## Filtering

`LogFilterProxyModel` wraps `LogTableModel` and supports three independent filters:

| Method | Default | Description |
|---|---|---|
| `set_severity_enabled(level, enabled)` | `INFO/WARN/ERROR` on | Show/hide rows by severity string |
| `set_type_mask(mask)` | all types on | Bitmask filter for the `type` column (app-supplied classifier) |
| `set_instrument(instrument)` | `None` (all) | Filter to a single instrument, or `None` for all |
| `set_notes_only(enabled)` | `False` | Show only rows with non-empty `details` |

The type-column classifier is injected at `LogTableModel` construction time, allowing each
app to define its own type taxonomy without touching the shared model:

```python
from shane_common.ui.log_viewer.log_table_model import LogTableModel

TYPE_SYSTEM   = 0b001
TYPE_TASK     = 0b010
TYPE_USER     = 0b100

def _classify(row):
    if row.kind.startswith("system."): return TYPE_SYSTEM
    if row.kind.startswith("task."):   return TYPE_TASK
    return TYPE_USER

model = LogTableModel(
    type_classifier=_classify,
    type_labels={TYPE_SYSTEM: "System", TYPE_TASK: "Task", TYPE_USER: "User"},
)
```

---

## Timezone display

The timestamp column header and all displayed times update live when the timezone is changed.

```python
# Set timezone on the model directly:
model.set_display_tz("UTC")                # header → "UTC Time"
model.set_display_tz("America/New_York")   # header → "NY Time"
model.set_display_tz("local")              # header → "Local Time"
```

`set_display_tz` emits both `dataChanged` (re-renders all timestamp cells) and
`headerDataChanged` (updates the column header). IANA timezone strings are resolved via
`zoneinfo.ZoneInfo`; `"local"` uses `datetime.now().astimezone().tzinfo`.

The log viewer windows in `trading_platform` expose this as a "Time:" `QComboBox`
(UTC / New York / Local) in the filter bar. Persisted via `SettingsManager` under
`app.display / display_timezone`.

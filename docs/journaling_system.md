# Journaling System

`shane_common` provides a domain-neutral JSONL journaling system. Apps register their own
taxonomy and disk layout via a **profile** and then emit structured events through a
**service** that fans out to one or more **sinks**.

`trading_platform` and `purity_app` both build on this layer. Trading-specific concepts
(broker, instrument, phase-gated kinds, strategy evaluation) never enter `shane_common`.

---

## Architecture overview

```
App startup
  └─ make_app_session()      ← run identity (run_id, pid, timestamps)
       │
       ├─ JournalProfile     ← app owns: kind taxonomy + disk layout
       ├─ JournalConfig      ← app_id, app_name, run_id
       ├─ JsonlJournalSink   ← routes envelopes to JSONL files on disk
       └─ JournalService     ← builds envelopes, fans out to sinks

       optional:
       ├─ SessionTailWriter  ← atomic JSON side-channel (last-seen timestamps)
       └─ EventGate          ← emit_on_change / emit_once / throttle helpers
```

Every journal line is a compact JSON object. `local_TS` is always the **first key**:

```json
{"local_TS":"2026-05-18T14:32:01.123456+10:00","ts":"2026-05-18T04:32:01.123456+00:00","run_id":"a1b2c3d4-...","kind":"system.start","source":{"app":"purity_app","component":"app"},"payload":{"pid":12345}}
```

---

## Packages

| Package | Purpose |
|---|---|
| `shane_common.sessions` | `AppSession`, `make_app_session`, `new_run_id`, `SessionTailWriter` |
| `shane_common.journaling` | `JournalProfile`, `JournalService`, `JournalConfig`, `JsonlJournalSink`, `build_journal_envelope`, `EventGate` |
| `shane_common.time` | `local_now_iso`, `utc_now_iso`, `local_iso_from_epoch`, `utc_iso_from_epoch` |

---

## Step 1 — App session

Create one `AppSession` per process at startup. It captures the `run_id` (UUID v4),
timestamps, PID, and root paths.

```python
from shane_common.sessions.app_session import make_app_session

session = make_app_session(
    app_id="my_app",
    app_name="MyApp",
    data_root=str(data_root),
    system_root=str(system_root),
)
# session.run_id  — stable for the lifetime of this process
# session.pid     — os.getpid()
# session.started_local_iso / started_utc_iso
```

---

## Step 2 — Journal profile

A `JournalProfile` tells the service three things:

1. **`validate_kind(kind)`** — is this kind allowed? Returns an error string or `None`.
2. **`stream_for_kind(kind)`** — which logical stream does this kind belong to?
   Used by the sink to choose the output filename.
3. **`route_event(envelope)`** — return the `Path` where this envelope should be appended.

```python
from pathlib import Path
from typing import Optional
from shane_common.journaling.profile import JournalProfile

_KINDS = frozenset({"system.start", "system.stop", "system.alive", "task.done"})

class MyAppProfile(JournalProfile):
    def __init__(self, data_root: Path) -> None:
        self._root = Path(data_root)

    def validate_kind(self, kind: str) -> Optional[str]:
        if kind not in _KINDS:
            return f"Unknown kind: {kind!r}"
        return None

    def stream_for_kind(self, kind: str) -> str:
        # "system.start" → "system", "task.done" → "task"
        return kind.split(".")[0] if "." in kind else "misc"

    def route_event(self, envelope: dict) -> Path:
        # Route by local date so directories match the user's wall-clock day
        day = envelope["local_TS"][:10]            # "2026-05-18"
        stream = self.stream_for_kind(envelope["kind"])
        return self._root / "_system" / "journals" / day / f"{stream}.jsonl"
```

**Routing tip:** use `envelope["local_TS"][:10]` (not UTC) for the day bucket in
user-facing apps so directories don't roll over at UTC midnight.

---

## Step 3 — Sink

`JsonlJournalSink` appends compact JSON lines to files returned by `profile.route_event`.
Parent directories are created automatically.

```python
from shane_common.journaling.jsonl_sink import JsonlJournalSink, JsonlJournalSinkConfig

sink = JsonlJournalSink(
    JsonlJournalSinkConfig(
        profile=profile,
        flush_each=True,   # flush after every write (safe default)
        fsync_each=False,  # True only if you need crash-safe durability
    )
)
```

The sink is thread-safe and holds open file handles per output path. Call `sink.close()`
at shutdown.

---

## Step 4 — Service

```python
from shane_common.journaling.service import JournalConfig, JournalService

cfg = JournalConfig(
    app_id="my_app",
    app_name="MyApp",
    run_id=session.run_id,
)

journal = JournalService(cfg, profile=profile, sinks=[sink])
```

**Optional: inject an app clock**

If the app has a logical clock (e.g. a replay clock that runs faster than wall time),
pass it as `clock`. The service calls `clock.now_ts` to populate the `ts` field.
`local_TS` always uses the real wall clock regardless.

```python
journal = JournalService(cfg, profile=profile, sinks=[sink], clock=app_clock)
```

---

## Step 5 — Emitting events

```python
journal.emit("system.start", {"pid": session.pid, "argv": sys.argv}, source="app")
journal.emit("task.done",    {"task_id": "t1", "duration_s": 1.2},  source="worker")
```

- `source` is a free-form component name string; it becomes `{"app": cfg.app_id, "component": source}` in the envelope.
- Sink failures never crash the caller — exceptions are printed via `traceback.print_exc()` and counted in `journal.emit_error_count()`.
- The service validates `kind` against the profile before building the envelope.

**With a message string:**

```python
journal.emit("system.stop", {"reason": "user_quit"}, source="app", msg="Shutting down")
```

**With correlation:**

```python
journal.emit("task.done", payload, source="worker",
             correlation={"task_id": "t1", "parent_run": session.run_id})
```

---

## Step 6 — Shutdown

```python
journal.emit("system.stop", {"reason": "normal"}, source="app")
journal.close_sinks()   # flushes and closes all open file handles
```

---

## Envelope schema

Every line written to disk has this field order (guaranteed by insertion-ordered dict):

| Key | Type | Notes |
|---|---|---|
| `local_TS` | str | Local ISO-8601 with UTC offset — **always first** |
| `ts` | str | UTC ISO-8601 (app clock if injected, else wall clock) |
| `run_id` | str | UUID v4 — stable for this process run |
| `kind` | str | Dot-separated taxonomy string, e.g. `"system.start"` |
| `source` | dict | `{"app": str, "component": str}` |
| `payload` | any | JSON-safe dict/value; NaN/Inf → null, datetime → ISO |
| `msg` | str | *(optional)* Human-readable message |
| `correlation` | dict | *(optional)* Cross-event linkage |

`local_TS` being first is a hard contract — do not reorder it. It makes the field
human-readable in raw file tails and log viewers without needing a JSON parser.

---

## Session tail writer

`SessionTailWriter` is a **side-channel** that writes a single JSON file tracking the most
recent timestamps seen by the process. It is independent of the journal service — useful
for supervisor/watchdog liveness checks without parsing JSONL.

```python
from shane_common.sessions.tail_writer import SessionTailWriter

tail = SessionTailWriter(system_root=str(system_root), run_id=session.run_id)

# Call periodically (e.g. from a 60-second timer):
tail.note_clock(time.time())
tail.note_event(time.time(), stream="system")

# Call at shutdown:
tail.flush(reason="system.stop")
```

The written file has `local_TS` as its first key:

```json
{
  "local_TS": "2026-05-18T14:32:01+10:00",
  "run_id": "a1b2c3d4-...",
  "last_clock_ts": 1747539121.4,
  "last_event_ts": 1747539121.4,
  "last_by_stream": {"system": 1747539121.4},
  "write_ts": 1747539121.4
}
```

The default path is `<system_root>/sessions/<run_id>/session_tail.<run_id>.json`.
Override `tail.path` before the first `flush()` to relocate it.

---

## Event gate

`EventGate` suppresses duplicate or high-frequency emissions. One instance per component.

```python
from shane_common.journaling.gate import EventGate

gate = EventGate()

# Fire only when the signature changes (e.g. status transitions):
gate.emit_on_change(
    key="chrome_status",
    signature=f"{allowed}:{reason}",
    emit_fn=lambda: journal.emit("chrome.allowed", {...}, source="watcher"),
)

# Fire only once per token (e.g. one startup event per run):
gate.emit_once(
    key="startup",
    token=session.run_id,
    emit_fn=lambda: journal.emit("system.start", {...}, source="app"),
)

# Fire at most once per interval:
gate.throttle(
    key="alive",
    now_ts=time.time(),
    min_interval_s=60.0,
    emit_fn=lambda: journal.emit("system.alive", {}, source="app"),
)
```

---

## Implementing for a new app — checklist

1. **Define kind constants** in `services/journaling_profile.py`:
   ```python
   KIND_SYSTEM_START = "system.start"
   # ...
   _MY_KINDS = frozenset({KIND_SYSTEM_START, ...})
   ```

2. **Subclass `JournalProfile`** with `validate_kind`, `stream_for_kind`, `route_event`.

3. **Create typed emitter functions** in `services/journal_events.py`:
   ```python
   def emit_app_started(j: JournalService, *, pid: int, argv: list) -> None:
       j.emit("system.start", {"pid": pid, "argv": list(argv)}, source="app")
   ```

4. **Assemble in a runtime factory**:
   ```python
   session = make_app_session(app_id=..., app_name=..., data_root=..., system_root=...)
   profile = MyAppProfile(data_root)
   sink    = JsonlJournalSink(JsonlJournalSinkConfig(profile=profile, flush_each=True))
   cfg     = JournalConfig(app_id=..., app_name=..., run_id=session.run_id)
   journal = JournalService(cfg, profile=profile, sinks=[sink])
   tail    = SessionTailWriter(system_root=str(system_root), run_id=session.run_id)
   ```

5. **Wire startup/shutdown** in the app entry point:
   - Emit `system.start` after journaling is ready.
   - On quit: emit `system.stop`, call `journal.close_sinks()`, `tail.flush(reason="system.stop")`.

6. **Add a periodic timer** (60 s) to call `tail.note_clock(time.time())` and emit
   `system.alive`.

---

## Relationship to `trading_platform`

`trading_platform/journaling/` is the trading-specific layer on top of this shared system.
It adds:

- `taxonomy.py` — trading kind strings (`decision.*`, `mkt.*`, `strategy.*`, etc.)
- `layout.py` — routing by broker + instrument + day bucket
- `envelope.py` — extended envelope with schema version `v` and `broker` in source
- `service.py` — `JournalingService` (public name; delegates to `JournalService` internals in a future migration)
- `adapters/` — domain-specific adapters for execution, feed health, run tail, strategy evaluation, subscriptions

`shane_common` never imports from `trading_platform`. The dependency only flows upward.

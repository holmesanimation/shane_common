# Supervisor System — Architecture & Reference

> **Last updated:** 2026-05-18

---

## Overview

The supervisor system is a multi-process watchdog that monitors trading and utility
applications by reading heartbeat files they write to a shared data directory.
It derives liveness states, emits transition-only alerts, persists an audit log,
and optionally restarts crashed processes.

The system is split into three tiers:

| Tier | Package | What lives here |
|---|---|---|
| Generic infrastructure | `shane_common.watchdog` | Heartbeat writer/reader, audit log, state store, process launcher, Qt tray base classes |
| Trading-specific supervisor | `trading_platform.supervisor` | Full supervisor service, rich liveness enum, trading heartbeat envelope, tray GUI, broker adapters |
| Application-level tray | `purity_app.gui.supervisor_tray` | Purity-specific tray and status window built on the generic base |

The guiding principle: **anything that does not reference broker state, trading models, or risk locks belongs in `shane_common`**.

---

## Directory Layout on Disk

All supervisor state lives under `<data_root>/_system/supervisor/` for the trading
supervisor, and `<data_root>/_system/purity/` for purity_app.

```
<data_root>/
  _system/
    supervisor/
      heartbeats/
        ib_trader.heartbeat.json        ← written by ib_trader app (HeartbeatWriter)
        kraken_trader.heartbeat.json    ← written by kraken_trader app
        ib_trader.exit_marker.json      ← written on clean app shutdown
      locks/
        global.lock.json                ← supervisor risk lock (read-write by service)
        supervisor.snapshot.json        ← per-app liveness snapshot (written each poll)
        supervisor.liveness.json        ← supervisor process own-liveness (written each poll)
        supervisor_tray.liveness.json   ← tray process liveness (written every 5 s)
      audit/
        supervisor.audit.jsonl          ← liveness transition events (append-only)
        supervisor.actions.jsonl        ← cancel/flatten action events
        supervisor.flatten.jsonl        ← flatten position events
        supervisor.reconciliation.jsonl ← post-restart reconciliation results
        supervisor.risk_mirror.jsonl    ← global risk mirror events
      intents/
        *.intent.json                   ← Telegram operator intents (read by service)
        *.result.json                   ← Telegram intent results (written by service)

    purity/
      heartbeats/
        purity_app.heartbeat.json       ← written by purity_app (future HeartbeatWriter)
        purity_app.exit_marker.json
      locks/
        purity_tray.liveness.json       ← purity tray liveness
      audit/
        purity.audit.jsonl              ← purity audit events
```

---

## Part 1 — `shane_common.watchdog` (Generic)

### Package: `shane_common/src/shane_common/watchdog/`

All classes here are framework-level and contain no trading-specific imports.

---

### `models.py`

Generic data models used across the watchdog layer.

| Symbol | Type | Description |
|---|---|---|
| `HEARTBEAT_SCHEMA_VERSION` | `int = 1` | Schema version written into every heartbeat file |
| `AppLiveness` | `str, Enum` | Seven generic liveness states: `UNKNOWN`, `STARTING`, `HEALTHY`, `STALE`, `DEAD`, `EXPECTED_EXIT`, `UNEXPECTED_EXIT` |
| `LivenessHeartbeat` | `dataclass` | Minimal heartbeat: `schema_version`, `app_id`, `pid`, `ts_wall_utc`, `seq` |
| `BaseLockState` | `dataclass` | Minimal lock: `locked`, `manual_intervention_required`, `reason` |

---

### `audit.py` — `AppendOnlyAuditLog`

Append-only JSONL audit log.

```python
from shane_common.watchdog.audit import AppendOnlyAuditLog
from pathlib import Path

log = AppendOnlyAuditLog(Path("/data/_system/audit/app.audit.jsonl"))
log.append({"ts_wall_utc": time.time(), "event": "something_happened"})
records = log.tail(50)   # list[dict], last 50 lines, oldest first
```

- Parent directory is created on first write.
- Errors call `report_exception` if available, otherwise `traceback.print_exc()`. Never silently swallowed.

---

### `heartbeat_writer.py` — `HeartbeatWriter`

Daemon thread that periodically writes `<app_id>.heartbeat.json` and a terminal
`<app_id>.exit_marker.json`.

```python
from shane_common.watchdog.heartbeat_writer import HeartbeatWriter

writer = HeartbeatWriter(
    app_id="my_app",
    heartbeats_dir=Path("/data/_system/my_app/heartbeats"),
    period_s=2.0,
)
writer.start()   # spawns daemon thread
# ... app runs ...
writer.stop()    # joins thread, writes terminal beat + exit marker
```

**Subclassing:** override `_build_payload(seq, ts_wall_utc) -> dict` to add custom fields.

**Thread safety:** one daemon thread owns all file I/O. Seq counter incremented under its own `Lock`.

**Atomic writes:** `os.replace()` via a sibling temp file. Retries on `PermissionError` (Windows-safe).

---

### `heartbeat_reader.py` — `HeartbeatReader`

Read-only reader for heartbeat + exit-marker files.

```python
from shane_common.watchdog.heartbeat_reader import HeartbeatReader

reader = HeartbeatReader(
    heartbeats_dir=Path("/data/_system/supervisor/heartbeats"),
    stale_s=10.0,
    dead_s=60.0,
)

heartbeat, mtime, exit_present = reader.read("ib_trader")
# heartbeat: Optional[LivenessHeartbeat] — None if absent/corrupt
# mtime: Optional[float] — file mtime as Unix timestamp
# exit_present: bool — True when exit_marker file exists

if mtime is not None:
    if reader.is_dead(mtime):
        print("DEAD")
    elif reader.is_stale(mtime):
        print("STALE")

app_ids = reader.list_known_app_ids()   # ["ib_trader", "kraken_trader"]
```

**Subclassing:** override `_parse_heartbeat(data: dict) -> LivenessHeartbeat` to deserialize richer domain-specific heartbeat envelopes.

---

### `state_store.py` — `WatchdogStateStore`

Manages snapshot and lock persistence for a watchdog process.

```python
from shane_common.watchdog.state_store import WatchdogStateStore

store = WatchdogStateStore(root_dir=Path("/data/_system/my_watchdog"))
# Creates: locks/, audit/ subdirectories automatically.

# Lock
lock = store.load_lock()               # BaseLockState; fail-closed on corrupt file
store.save_lock(lock)                  # atomic write

# Snapshot
snap = store.load_snapshot()           # dict; {} on absent/corrupt
store.save_snapshot({"key": "val"})    # atomic write

# Audit
store.append_audit({"event": "..."})
records = store.tail_audit(50)
```

**Subclassing:** override `_parse_lock(data)` / `_lock_to_dict(lock)` to use richer lock types.

---

### `process_launcher.py` — `ProcessLauncher`

Spawns fully detached child processes with rate limiting and cooldown.

```python
from shane_common.watchdog.process_launcher import ProcessLauncher, ProcessLaunchConfig

launcher = ProcessLauncher(
    pre_launch_hook=lambda app_id: print(f"About to launch {app_id}"),
    post_launch_hook=lambda app_id: print(f"Launched {app_id}"),
)

cfg = ProcessLaunchConfig(
    app_id="ib_trader",
    launch_cmd=["python", "ib_trader/IB_MAIN.py"],
    max_restarts_per_hour=3,
    cooldown_s=30.0,
    enabled=True,
)

launched = launcher.maybe_restart(cfg)   # True if spawn was attempted
```

- Windows: uses `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`.
- Non-Windows: uses `start_new_session=True`.

---

### `tray/` — Qt Abstractions

> **Requires PySide6.** Install with `pip install shane_common[qt]`

#### `tray/icons.py`

```python
from shane_common.watchdog.tray.icons import (
    make_circle_icon,
    worst_case_color,
    COLOR_RED, COLOR_ORANGE, COLOR_YELLOW, COLOR_GREEN, COLOR_BLUE, COLOR_GRAY,
)

icon = make_circle_icon(COLOR_GREEN, size=32)   # QIcon with anti-aliased circle

color = worst_case_color(
    {"app_a": "DEAD", "app_b": "HEALTHY"},
    urgent_states=frozenset({"DEAD", "UNEXPECTED_EXIT"}),
    warning_states=frozenset({"STALE"}),
    info_states=frozenset({"EXPECTED_EXIT"}),
    healthy_states=frozenset({"HEALTHY", "STARTING"}),
)   # → COLOR_RED
```

#### `tray/liveness_widget.py` — `LivenessBadge`

Colored QLabel pill.

```python
from shane_common.watchdog.tray.liveness_widget import LivenessBadge

badge = LivenessBadge(
    color_fn=lambda state: {"HEALTHY": "#22c55e"}.get(state, "#94a3b8"),
    light_bg_states=frozenset({"STARTING"}),
)
badge.set_liveness("HEALTHY")
```

#### `tray/audit_panel.py` — `AuditPanel`

Scrollable table of the last N JSONL audit records.

```python
from shane_common.watchdog.tray.audit_panel import AuditPanel

panel = AuditPanel(audit_path=Path("/data/audit.jsonl"), tail_n=50)
panel.refresh()   # re-reads file; call on a QTimer
```

Columns: **Time**, **App**, **Event**.

#### `tray/base_window.py` — `BaseStatusWindow`

`QMainWindow` base for watchdog status dashboards. Hides on close (does not quit).

```python
from shane_common.watchdog.tray.base_window import BaseStatusWindow

class MyStatusWindow(BaseStatusWindow):
    def _build_content(self, container):
        layout = QVBoxLayout(container)
        layout.addWidget(QLabel("Hello"))

win = MyStatusWindow(settings_org="myapp", settings_app="MyMonitor", title="Status")
win.show_and_raise()
```

- `closeEvent` ignores the event and calls `hide()`.
- `refresh_requested` Signal emitted by the Refresh Now button.
- `auto_refresh_enabled` property toggled by the checkbox.
- Geometry persisted via QSettings.

#### `tray/base_tray_app.py` — `BaseTrayApp`

The core tray scaffold. All supervisor tray applications subclass this.

**Subclass checklist:**

1. Override `_app_id` property → unique string ID used in audit records.
2. Override `_audit_log` property → return `AppendOnlyAuditLog` or `None`.
3. Override `_poll()` → read state from disk, update `self._tray` icon/tooltip.
4. Optionally override `_liveness_payload()` → dict written to the liveness file.
5. Build context menu items on `self._menu` *before* calling `super().start()`.
6. Call `super().start()` at the end of your `start()` override.

```python
from shane_common.watchdog.tray.base_tray_app import BaseTrayApp
from shane_common.watchdog.audit import AppendOnlyAuditLog

class MyTrayApp(BaseTrayApp):
    def __init__(self, data_root, parent=None):
        super().__init__(
            liveness_path=data_root / "locks" / "my_tray.liveness.json",
            settings_org="myorg",
            settings_app="MyMonitor",
            parent=parent,
        )
        show_action = self._menu.addAction("Show")
        show_action.triggered.connect(self._on_show_hide)
        self._menu.addSeparator()

    @property
    def _app_id(self):
        return "my_tray"

    @property
    def _audit_log(self):
        return AppendOnlyAuditLog(self._liveness_path.parent.parent / "audit" / "app.audit.jsonl")

    def _poll(self):
        # Read state; update self._tray.setIcon(...) and self._tray.setToolTip(...)
        pass

    def _on_show_hide(self):
        pass
```

**What `BaseTrayApp` manages automatically:**
- `QSystemTrayIcon` creation and `activated` signal (left/double-click → `_on_show_hide`).
- Context menu with **Quit** appended via `_add_quit_to_menu()` during `start()`.
- Poll timer (`self._poll_timer`, default 2000 ms).
- Liveness write timer (`self._liveness_timer`, default 5000 ms).
- `QApplication.aboutToQuit` wired to `_record_app_quit("operator_quit")` AND `_save_prefs()`.
- `request_quit(reason)` — call for any explicit operator quit action; records the specific reason before calling `QApplication.quit()`.
- `_record_app_quit(reason)` — idempotent; only writes one audit record per process lifetime.
- QSettings geometry persistence via `_save_prefs()` / `_restore_prefs()`.

---

## Part 2 — `trading_platform.supervisor` (Trading-Specific)

### Entry Points

| Script | BAT launcher | Purpose |
|---|---|---|
| `trading_platform/scripts/run_supervisor.py` | `run_SUPERVISOR.bat` | Main supervisor service process |
| `trading_platform/scripts/run_supervisor_tray.py` | `run_supervisor_tray.bat` | Tray GUI (read-only observer) |
| `trading_platform/scripts/run_supervisor_telegram_bot.py` | `run_supervisor_telegram_bot.bat` | Telegram operator bot |

All three use Windows named mutexes for single-instance enforcement.

**Quick start:**
```powershell
# 1. Start supervisor service (background, no GUI)
.\run_SUPERVISOR.bat

# 2. Start tray GUI (system tray icon + dashboard)
.\run_supervisor_tray.bat

# Optional: Telegram bot
.\run_supervisor_telegram_bot.bat
```

The tray and the service are **independent processes** — both can be started and
stopped independently. The tray shows UNKNOWN for all apps until the service
starts writing snapshot files.

---

### `supervisor_service.py` — `SupervisorService`

The blocking poll loop.

```python
service = SupervisorService(
    config=config,
    state_store=state_store,
    reader=reader,
    alerter=alerter,
)
service.run()   # blocks; handles SIGINT/SIGTERM for graceful stop
```

**What it does each poll cycle (`_poll_once`):**
1. Reads each app's heartbeat file (`HeartbeatReader.read`).
2. Derives `SupervisorAppLiveness` state via `_derive_liveness()` (pure function).
3. Applies broker adapter enrichment (`IBSupervisorAdapter`, `KrakenSupervisorAdapter`).
4. On **state transition**: appends to audit log + emits notification (transition-only invariant).
5. Attempts process restart via `ProcessLauncher.maybe_restart` (gated; Phase 2).
6. Attempts cancel-orders fail-safe (gated; Phase 6).
7. Attempts flatten-positions fail-safe (gated; Phase 7).
8. Saves atomic snapshot to `supervisor.snapshot.json`.
9. Checks tray liveness (DEGRADED_UX only; no safety action).
10. Updates risk mirror (`RiskMirrorMonitor`; Phase 8).
11. Processes Telegram operator intents.
12. Writes own liveness to `supervisor.liveness.json`.

**Transition-only invariant:** `alerter.emit_transition()` is called ONLY when
`new_liveness != prev_liveness`. The same state on consecutive polls never
produces a repeated alert.

---

### `supervisor_guard.py` — `SupervisorGuard`

In-app daemon thread that watches the supervisor itself and auto-launches it if
the `supervisor.liveness.json` file goes stale.

```python
from trading_platform.supervisor.supervisor_guard import SupervisorGuard

guard = SupervisorGuard(
    data_root=data_root,
    notify_manager=notify_manager,
    check_period_s=60.0,
    liveness_stale_s=30.0,
    max_launch_attempts=3,
)
guard.start()   # daemon thread; returns immediately
```

Escalation:
- First miss → no action (suppresses write lag during startup).
- Second consecutive miss → attempt launch + emit WARNING.
- `max_launch_attempts` reached → emit URGENT once; stop trying.

---

### `models.py` — Trading Liveness Enum

`SupervisorAppLiveness` has 18 states (vs. 7 in `AppLiveness`):

| State | Severity | Meaning |
|---|---|---|
| `UNKNOWN` | — | Never seen a heartbeat |
| `STARTING` | — | App in startup phase |
| `HEALTHY` | — | Fresh heartbeat, all loops alive |
| `SAFE_MODE` | — | Safe mode flag set in heartbeat |
| `DEGRADED_UX` | INFO | UI degraded; trading continues |
| `STALE` | INFO | Heartbeat file age > stale threshold |
| `EXPECTED_EXIT` | INFO | Exit marker present + app stopping |
| `DEGRADED_TRADING` | WARNING | App reports degraded trading state |
| `BROKER_UNAVAILABLE` | WARNING | Broker disconnected beyond grace period |
| `FROZEN_MAIN_THREAD` | WARNING | Qt main tick counter stale |
| `RISK_LOCKED` | WARNING | Risk lock flag set in heartbeat |
| `RECOVERY_PENDING` | WARNING | Post-restart reconciliation pending |
| `FROZEN_ASYNCIO` | URGENT | Asyncio loop tick counter stale |
| `FROZEN_STRATEGY_LOOP` | URGENT | Strategy loop tick counter stale |
| `PROCESS_DEAD` | URGENT | File very stale + PID gone |
| `UNEXPECTED_EXIT` | URGENT | Stale beyond exit grace, no marker, PID gone |
| `MANUAL_INTERVENTION_REQUIRED` | URGENT | Explicit lock set by service |
| `AUTH_REQUIRED` | URGENT | IB process running but API port unresponsive (2FA pending) |

---

### `heartbeat_writer.py` (trading-specific) — `HeartbeatWriter`

The trading-specific writer wraps `snapshot_fn: Callable[[], HeartbeatEnvelope]` and
is much richer than the generic one. It also has three tick-bump methods:

```python
from trading_platform.supervisor.heartbeat_writer import HeartbeatWriter

writer = HeartbeatWriter(
    app_id="ib_trader",
    heartbeats_dir=state_store.heartbeats_dir,
    snapshot_fn=lambda: app.build_heartbeat_envelope(),
    period_s=2.0,
)
writer.start()
# In Qt main loop (QTimer 50 ms):
writer.bump_qt_main()
# In asyncio task (50 ms):
writer.bump_asyncio()
# In strategy evaluation iteration:
writer.bump_strategy_loop()
# On clean shutdown:
writer.stop(exit_intent=ExitIntent.GRACEFUL)
```

The bump methods update monotonic tick timestamps independently of the file write
period, allowing the supervisor to detect frozen loops even while the heartbeat
file is still being written.

---

### `tray/client.py` — `SupervisorTrayClient`

Read-mostly client for the tray GUI. Wraps all disk reads and the one write
operation (lock clear) available to the tray process.

```python
from trading_platform.supervisor.tray.client import SupervisorTrayClient

client = SupervisorTrayClient(data_root)

snapshot = client.load_snapshot()           # SupervisorSnapshot
lock = client.load_lock()                   # SupervisorLockState
hb, mtime, exit_ok = client.read_heartbeat("ib_trader")
client.clear_lock(reason="manual_reset")    # only write op available to tray
audit = client.tail_audit(50)               # list[dict]
client.append_tray_audit({"event": "..."})  # write quit/operator records
```

---

### `tray/tray_app.py` — `SupervisorTrayApp`

Subclasses `BaseTrayApp`. Provides the full trading supervisor tray GUI.

```python
from trading_platform.supervisor.tray.tray_app import SupervisorTrayApp

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)
tray = SupervisorTrayApp(config=config, client=client)
tray.start()
sys.exit(app.exec())
```

**Modes:**
- **Compact** (default): a small always-on-top frameless `StatusPill` overlay.
- **Expanded**: full `SupervisorWindow` dashboard with per-app liveness rows, lock panel,
  recent events, and domain state badges.

Toggle with left-click on the tray icon, double-click, or the pill context menu.

**Tray icon colors:**
- Green — all apps HEALTHY/STARTING/SAFE_MODE
- Yellow — any INFO state
- Orange — any WARNING state
- Red — any URGENT state
- Gray — no snapshot yet

---

### `tray/icons.py` — Trading Icons

Re-exports `make_circle_icon` from `shane_common`. Adds trading-specific badge helpers.

```python
from trading_platform.supervisor.tray.icons import (
    make_circle_icon,        # QIcon filled circle
    worst_case_color,        # dict[str, SupervisorAppLiveness] → hex color
    liveness_hex,            # SupervisorAppLiveness → hex
    liveness_badge_style,    # SupervisorAppLiveness → QLabel stylesheet
    domain_hex,              # DomainState → hex
    domain_badge_style,      # DomainState → QLabel stylesheet
)
```

---

### `config.py` — `SupervisorConfig`

Loaded from `trading_platform/configs/supervisor.yaml`.

Key sections:

| Section | Purpose |
|---|---|
| `data_root` | Root of the `_system/supervisor/` tree |
| `poll_period_s` | How often the service polls (default 5 s) |
| `apps[]` | Per-app: `app_id`, `broker_id`, `launch_cmd`, `tws_port`, restart policy, action gates |
| `heartbeat.*` | `stale_s`, `frozen_s`, `exit_grace_s`, `tick_stale_s`, `broker_grace_s`, `flap_suppress_s` |
| `restart_policy.*` | `enabled`, `max_restarts_per_hour`, `cooldown_s` |
| `reconciliation.*` | `enabled` |
| `risk_mirror.*` | `enabled`, `daily_loss_limit`, `drawdown_limit` |
| `brokers.ib.*` | `tws_path`, `gateway_path`, `api_host`, `api_port`, `auto_launch` |
| `brokers.kraken.*` | `api_health_endpoint` |
| `telegram.*` | `enabled`, `bot_token_env`, `allowed_user_ids`, `alerts.*` |
| `notify.*` | Bark and/or Telegram notification adapter config |

---

### `alerting.py` — `SupervisorAlerter`

Emits transition-only notifications. Severity is derived from `SupervisorAppLiveness`.
Flap suppression prevents the same transition from firing twice within `flap_suppress_s`.

```python
alerter.emit_transition(
    app_id="ib_trader",
    prev_state=SupervisorAppLiveness.HEALTHY,
    new_state=SupervisorAppLiveness.STALE,
    snapshot=envelope,
    details="state=STALE; file_age=12.4s",
)
```

---

### `state_store.py` — `SupervisorStateStore`

Owns all persistent state for the trading supervisor service.

```python
store = SupervisorStateStore(data_root)

store.save_snapshot(snap)
store.load_lock()
store.save_lock(lock)
store.append_audit(audit_record)
store.save_supervisor_liveness(liveness_record)
store.read_tray_liveness()    # (record, mtime)
```

Audit files are kept open with `buffering=1` (line-buffered) and protected by an
`RLock` so concurrent writes from multiple threads are safe.

---

## Part 3 — `purity_app` Supervisor Tray

### `purity_app/services/supervisor_client.py` — `PuritySupervisorClient`

Lightweight read/write adapter for purity_app liveness.

```python
from purity_app.services.supervisor_client import PuritySupervisorClient

client = PuritySupervisorClient(data_root=Path.home() / ".purity")

hb, mtime, exit_present = client.heartbeat_reader.read("purity_app")
client.audit_log.append({"event": "started"})
```

Default paths:
- Heartbeats: `<data_root>/_system/purity/heartbeats/`
- Audit: `<data_root>/_system/purity/audit/purity.audit.jsonl`

---

### `purity_app/gui/supervisor_tray.py`

| Class | Superclass | Purpose |
|---|---|---|
| `PurityStatusWindow` | `BaseStatusWindow` | Heartbeat age + liveness status window |
| `PurityTrayApp` | `BaseTrayApp` | Tray icon that shows purity_app health |

`PurityTrayApp` is created and started automatically in `purity_app/app.py`:

```python
purity_tray = PurityTrayApp(data_root)
purity_tray.start()
```

Override `PURITY_DATA_ROOT` environment variable to use a non-default data root.

---

## How To: Integrate HeartbeatWriter into a New App

1. Create a `SupervisorStateStore` (trading) or use the directory directly.
2. Instantiate `HeartbeatWriter` with `snapshot_fn` returning a `HeartbeatEnvelope`.
3. Call `writer.start()` after the app's main objects are created.
4. Wire `bump_qt_main()` to a 50 ms `QTimer`.
5. Wire `bump_asyncio()` to an asyncio periodic task.
6. Wire `bump_strategy_loop()` inside the strategy evaluation loop.
7. Call `writer.stop(ExitIntent.GRACEFUL)` in the shutdown path.

Minimal non-trading example using the generic `HeartbeatWriter`:

```python
from shane_common.watchdog.heartbeat_writer import HeartbeatWriter

writer = HeartbeatWriter("my_daemon", Path("/data/heartbeats"))
writer.start()
try:
    while True:
        time.sleep(1)
finally:
    writer.stop()
```

---

## How To: Create a New Supervisor Tray (Non-Trading)

1. Subclass `BaseTrayApp`.
2. Implement `_app_id`, `_audit_log`, `_poll()`.
3. Add context menu items on `self._menu` before calling `super().start()`.
4. Create a `QApplication`, call `tray.start()`, call `app.exec()`.

See `purity_app/gui/supervisor_tray.py` for a complete minimal example.

---

## How To: Create a New Status Window

1. Subclass `BaseStatusWindow`.
2. Override `_build_content(container)` to add domain-specific widgets.
3. Call `show_and_raise()` to show; `hide()` to hide. `closeEvent` hides automatically.
4. Connect your tray's `_poll()` to call `window.refresh()` when `window.isVisible()`.

---

## File Protocol Reference

### Heartbeat file (`<app_id>.heartbeat.json`)

Written every 2 s by `HeartbeatWriter`. Minimal schema:

```json
{
  "schema_version": 1,
  "app_id": "ib_trader",
  "pid": 12345,
  "ts_wall_utc": 1747600000.0,
  "seq": 42
}
```

Trading apps include the full `HeartbeatEnvelope` with ~30 additional fields
(app state, connection state, domain states, PnL, position counts, tick counters).

### Exit marker file (`<app_id>.exit_marker.json`)

Written by `HeartbeatWriter.stop()` on clean shutdown:

```json
{
  "app_id": "ib_trader",
  "ts_wall_utc": 1747600010.0,
  "pid": 12345
}
```

### Snapshot file (`supervisor.snapshot.json`)

Written after every poll cycle:

```json
{
  "ts_wall_utc": 1747600000.0,
  "app_states": {"ib_trader": "HEALTHY", "kraken_trader": "STALE"},
  "last_heartbeat_ts": {"ib_trader": 1747599998.2}
}
```

### Lock file (`global.lock.json`)

Fail-closed: absent → all-clear. Corrupt → fully locked.

```json
{
  "schema_version": 1,
  "ts_wall_utc": 1747600000.0,
  "global_risk_locked": false,
  "recovery_pending": false,
  "safe_mode_required": false,
  "manual_intervention_required": false,
  "set_by": "operator",
  "reason": "manual reset via tray"
}
```

### Audit JSONL (`supervisor.audit.jsonl`)

One JSON object per line. Append-only. Each line is a state transition event:

```json
{"ts_wall_utc":1747600000.0,"app_id":"ib_trader","prev_liveness":"UNKNOWN","new_liveness":"STARTING","heartbeat":null,"note":"UNKNOWN → STARTING"}
{"ts_wall_utc":1747600020.0,"app_id":"ib_trader","prev_liveness":"STARTING","new_liveness":"HEALTHY","heartbeat":{...},"note":"STARTING → HEALTHY"}
```

Tray quit events have `"event": "app_tray_quit"` and `"reason"` field.

---

## Tests

```powershell
# Generic watchdog tests (25 tests — no Qt, no trading deps)
python -m pytest shane_common/tests/watchdog/ -v

# Purity supervisor client tests (5 tests)
python -m pytest purity_app/tests/test_purity_supervisor_client.py -v
```

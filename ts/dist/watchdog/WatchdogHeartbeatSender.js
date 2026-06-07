"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.WatchdogHeartbeatSender = void 0;
class WatchdogHeartbeatSender {
    _seq = 0;
    _timer = null;
    _appId;
    _pid;
    _appState;
    _periodMs;
    _sendCommand;
    constructor(opts) {
        this._appId = opts.appId;
        this._pid = opts.pid;
        this._appState = opts.appState ?? 'HEALTHY';
        this._periodMs = opts.periodMs ?? 2000;
        this._sendCommand = opts.sendCommand;
    }
    start() {
        if (this._timer !== null) {
            return; // already running
        }
        this._send();
        this._timer = setInterval(() => this._send(), this._periodMs);
    }
    stop() {
        if (this._timer !== null) {
            clearInterval(this._timer);
            this._timer = null;
        }
    }
    setAppState(state) {
        this._appState = state;
    }
    _send() {
        this._seq += 1;
        const params = {
            app_id: this._appId,
            pid: this._pid,
            app_state: this._appState,
            seq: this._seq,
            ts: Date.now() / 1000,
        };
        this._sendCommand('watchdog.heartbeat', params);
    }
}
exports.WatchdogHeartbeatSender = WatchdogHeartbeatSender;
//# sourceMappingURL=WatchdogHeartbeatSender.js.map
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.LogForwarder = void 0;
class LogForwarder {
    _buffer = [];
    _connected = false;
    _sendCommand = null;
    _maxBuffer = 500;
    setSendCommand(fn) {
        this._sendCommand = fn;
    }
    onConnected() {
        this._connected = true;
        const drained = this._buffer.splice(0);
        for (const entry of drained) {
            this._flush(entry);
        }
    }
    onDisconnected() {
        this._connected = false;
    }
    write(level, message, source) {
        const entry = {
            level,
            message,
            source,
            ts: Date.now() / 1000,
        };
        if (this._connected && this._sendCommand) {
            this._flush(entry);
        }
        else {
            if (this._buffer.length >= this._maxBuffer) {
                this._buffer.shift();
            }
            this._buffer.push(entry);
        }
    }
    _flush(entry) {
        if (this._sendCommand) {
            this._sendCommand('log.write', entry);
        }
    }
}
exports.LogForwarder = LogForwarder;
//# sourceMappingURL=LogForwarder.js.map
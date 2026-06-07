"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.ConsoleCapture = void 0;
let _originals = null;
class ConsoleCapture {
    static start(forwarder) {
        if (_originals !== null) {
            return; // already started — idempotent
        }
        _originals = {
            log: console.log.bind(console),
            info: console.info.bind(console),
            warn: console.warn.bind(console),
            error: console.error.bind(console),
            debug: console.debug.bind(console),
        };
        const wrap = (level, orig) => (...args) => {
            orig(...args);
            forwarder.write(level, args.map(String).join(' '), 'electron.main');
        };
        console.log = wrap('INFO', _originals.log);
        console.info = wrap('INFO', _originals.info);
        console.warn = wrap('WARN', _originals.warn);
        console.error = wrap('ERROR', _originals.error);
        console.debug = wrap('DEBUG', _originals.debug);
    }
    static stop() {
        if (_originals === null) {
            return; // not started — idempotent
        }
        console.log = _originals.log;
        console.info = _originals.info;
        console.warn = _originals.warn;
        console.error = _originals.error;
        console.debug = _originals.debug;
        _originals = null;
    }
}
exports.ConsoleCapture = ConsoleCapture;
//# sourceMappingURL=ConsoleCapture.js.map
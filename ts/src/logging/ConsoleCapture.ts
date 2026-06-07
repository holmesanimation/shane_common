import { LogForwarder } from './LogForwarder';
import { LogWriteParams } from '../contracts/commands';

type ConsoleFn = (...args: unknown[]) => void;

interface OriginalFns {
  log:   ConsoleFn;
  info:  ConsoleFn;
  warn:  ConsoleFn;
  error: ConsoleFn;
  debug: ConsoleFn;
}

let _originals: OriginalFns | null = null;

export class ConsoleCapture {
  static start(forwarder: LogForwarder): void {
    if (_originals !== null) {
      return; // already started — idempotent
    }

    _originals = {
      log:   console.log.bind(console),
      info:  console.info.bind(console),
      warn:  console.warn.bind(console),
      error: console.error.bind(console),
      debug: console.debug.bind(console),
    };

    const wrap = (level: LogWriteParams['level'], orig: ConsoleFn) =>
      (...args: unknown[]): void => {
        orig(...args);
        forwarder.write(level, args.map(String).join(' '), 'electron.main');
      };

    console.log   = wrap('INFO',  _originals.log);
    console.info  = wrap('INFO',  _originals.info);
    console.warn  = wrap('WARN',  _originals.warn);
    console.error = wrap('ERROR', _originals.error);
    console.debug = wrap('DEBUG', _originals.debug);
  }

  static stop(): void {
    if (_originals === null) {
      return; // not started — idempotent
    }
    console.log   = _originals.log;
    console.info  = _originals.info;
    console.warn  = _originals.warn;
    console.error = _originals.error;
    console.debug = _originals.debug;
    _originals = null;
  }
}

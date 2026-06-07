import { LogWriteParams } from '../contracts/commands';

export type SendCommandFn = (command: string, params: Record<string, unknown>) => void;

export class LogForwarder {
  private _buffer: Array<LogWriteParams> = [];
  private _connected = false;
  private _sendCommand: SendCommandFn | null = null;
  private readonly _maxBuffer = 500;

  setSendCommand(fn: SendCommandFn): void {
    this._sendCommand = fn;
  }

  onConnected(): void {
    this._connected = true;
    const drained = this._buffer.splice(0);
    for (const entry of drained) {
      this._flush(entry);
    }
  }

  onDisconnected(): void {
    this._connected = false;
  }

  write(level: LogWriteParams['level'], message: string, source: string): void {
    const entry: LogWriteParams = {
      level,
      message,
      source,
      ts: Date.now() / 1000,
    };
    if (this._connected && this._sendCommand) {
      this._flush(entry);
    } else {
      if (this._buffer.length >= this._maxBuffer) {
        this._buffer.shift();
      }
      this._buffer.push(entry);
    }
  }

  private _flush(entry: LogWriteParams): void {
    if (this._sendCommand) {
      this._sendCommand('log.write', entry as unknown as Record<string, unknown>);
    }
  }
}

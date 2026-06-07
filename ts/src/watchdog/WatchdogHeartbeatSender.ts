import { WatchdogHeartbeatParams } from '../contracts/commands';
import { SendCommandFn } from '../logging/LogForwarder';

export interface WatchdogHeartbeatSenderOptions {
  appId: string;
  pid: number;
  appState?: string;
  periodMs?: number;
  sendCommand: SendCommandFn;
}

export class WatchdogHeartbeatSender {
  private _seq = 0;
  private _timer: ReturnType<typeof setInterval> | null = null;
  private readonly _appId: string;
  private readonly _pid: number;
  private _appState: string;
  private readonly _periodMs: number;
  private readonly _sendCommand: SendCommandFn;

  constructor(opts: WatchdogHeartbeatSenderOptions) {
    this._appId      = opts.appId;
    this._pid        = opts.pid;
    this._appState   = opts.appState ?? 'HEALTHY';
    this._periodMs   = opts.periodMs ?? 2000;
    this._sendCommand = opts.sendCommand;
  }

  start(): void {
    if (this._timer !== null) {
      return; // already running
    }
    this._send();
    this._timer = setInterval(() => this._send(), this._periodMs);
  }

  stop(): void {
    if (this._timer !== null) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  setAppState(state: string): void {
    this._appState = state;
  }

  private _send(): void {
    this._seq += 1;
    const params: WatchdogHeartbeatParams = {
      app_id:    this._appId,
      pid:       this._pid,
      app_state: this._appState,
      seq:       this._seq,
      ts:        Date.now() / 1000,
    };
    this._sendCommand('watchdog.heartbeat', params as unknown as Record<string, unknown>);
  }
}

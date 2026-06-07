import { SendCommandFn } from '../logging/LogForwarder';
export interface WatchdogHeartbeatSenderOptions {
    appId: string;
    pid: number;
    appState?: string;
    periodMs?: number;
    sendCommand: SendCommandFn;
}
export declare class WatchdogHeartbeatSender {
    private _seq;
    private _timer;
    private readonly _appId;
    private readonly _pid;
    private _appState;
    private readonly _periodMs;
    private readonly _sendCommand;
    constructor(opts: WatchdogHeartbeatSenderOptions);
    start(): void;
    stop(): void;
    setAppState(state: string): void;
    private _send;
}
//# sourceMappingURL=WatchdogHeartbeatSender.d.ts.map
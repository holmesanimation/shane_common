import { LogWriteParams } from '../contracts/commands';
export type SendCommandFn = (command: string, params: Record<string, unknown>) => void;
export declare class LogForwarder {
    private _buffer;
    private _connected;
    private _sendCommand;
    private readonly _maxBuffer;
    setSendCommand(fn: SendCommandFn): void;
    onConnected(): void;
    onDisconnected(): void;
    write(level: LogWriteParams['level'], message: string, source: string): void;
    private _flush;
}
//# sourceMappingURL=LogForwarder.d.ts.map
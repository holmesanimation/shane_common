export interface IpcTrafficRecord {
    direction: 'incoming' | 'outgoing';
    ts: number;
    topic: string;
    kind: string;
    seq: number;
    payloadPreview?: Record<string, unknown>;
}
export interface IpcTrafficFileWriterOptions {
    /** Absolute path to the temp file written before a session is established. */
    filePath: string;
}
/**
 * Appends every IPC TrafficRecord to a CSV file as it arrives.
 *
 * Lifecycle:
 *  1. Constructed with a timestamp-based temp path — starts writing immediately.
 *  2. On `relocate(newPath)`: copies the current file to `newPath`, then
 *     switches all future appends to `newPath`.  The original temp file is
 *     deleted after a successful copy.
 */
export declare class IpcTrafficFileWriter {
    private _filePath;
    private _headerWritten;
    constructor(opts: IpcTrafficFileWriterOptions);
    /**
     * Copy the current file to `newPath` and switch to appending there.
     * Safe to call once; subsequent calls are no-ops if `newPath` is the same.
     */
    relocate(newPath: string): void;
    write(record: IpcTrafficRecord): void;
    private _ensureDir;
}
//# sourceMappingURL=IpcTrafficFileWriter.d.ts.map
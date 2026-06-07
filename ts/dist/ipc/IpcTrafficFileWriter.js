"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.IpcTrafficFileWriter = void 0;
const fs = require("node:fs");
const path = require("node:path");
const _HEADER = 'Time,Direction,Kind,Topic,Seq,PayloadPreview\n';
/**
 * Appends every IPC TrafficRecord to a CSV file as it arrives.
 *
 * Lifecycle:
 *  1. Constructed with a timestamp-based temp path — starts writing immediately.
 *  2. On `relocate(newPath)`: copies the current file to `newPath`, then
 *     switches all future appends to `newPath`.  The original temp file is
 *     deleted after a successful copy.
 */
class IpcTrafficFileWriter {
    _filePath;
    _headerWritten = false;
    constructor(opts) {
        this._filePath = opts.filePath;
    }
    /**
     * Copy the current file to `newPath` and switch to appending there.
     * Safe to call once; subsequent calls are no-ops if `newPath` is the same.
     */
    relocate(newPath) {
        if (newPath === this._filePath)
            return;
        try {
            const dir = path.dirname(newPath);
            fs.mkdirSync(dir, { recursive: true });
            if (this._headerWritten) {
                // Copy accumulated content to new location
                fs.copyFileSync(this._filePath, newPath);
                // Delete the temp file
                try {
                    fs.unlinkSync(this._filePath);
                }
                catch { /* ignore */ }
            }
            console.log(`[IpcTrafficFileWriter] relocated: ${this._filePath} → ${newPath}`);
            this._filePath = newPath;
        }
        catch (err) {
            console.warn(`[IpcTrafficFileWriter] relocate failed: ${err}`);
        }
    }
    write(record) {
        if (!this._headerWritten) {
            this._ensureDir();
            fs.appendFileSync(this._filePath, _HEADER, 'utf-8');
            this._headerWritten = true;
            console.log(`[IpcTrafficFileWriter] opened: ${this._filePath}`);
        }
        const ts = record.ts > 0 ? _formatTs(record.ts) : '';
        const preview = JSON.stringify(record.payloadPreview ?? {});
        const row = `${_csvCell(ts)},${_csvCell(record.direction)},${_csvCell(record.kind)},` +
            `${_csvCell(record.topic)},${record.seq},${_csvCell(preview)}\n`;
        fs.appendFileSync(this._filePath, row, 'utf-8');
    }
    _ensureDir() {
        const dir = path.dirname(this._filePath);
        fs.mkdirSync(dir, { recursive: true });
    }
}
exports.IpcTrafficFileWriter = IpcTrafficFileWriter;
function _formatTs(epochSeconds) {
    const d = new Date(epochSeconds * 1000);
    const pad = (n, w = 2) => String(n).padStart(w, '0');
    return (`${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
        `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}.` +
        `${pad(d.getUTCMilliseconds(), 3)}`);
}
function _csvCell(s) {
    if (s.includes(',') || s.includes('"') || s.includes('\n')) {
        return '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
}
//# sourceMappingURL=IpcTrafficFileWriter.js.map
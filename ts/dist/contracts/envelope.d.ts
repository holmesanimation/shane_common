export interface ApiEnvelope<T> {
    kind: string;
    ts: number;
    seq: number;
    lane: 'lossless' | 'lossy';
    schema_version: number;
    payload: T;
}
//# sourceMappingURL=envelope.d.ts.map
export declare const AppLiveness: {
    readonly UNKNOWN: "UNKNOWN";
    readonly STARTING: "STARTING";
    readonly HEALTHY: "HEALTHY";
    readonly STALE: "STALE";
    readonly DEAD: "DEAD";
    readonly EXPECTED_EXIT: "EXPECTED_EXIT";
    readonly UNEXPECTED_EXIT: "UNEXPECTED_EXIT";
};
export type AppLiveness = typeof AppLiveness[keyof typeof AppLiveness];
export interface HeartbeatRecord {
    app_id: string;
    pid: number;
    seq: number;
    ts: number;
    app_state: string;
    schema_version: number;
}
//# sourceMappingURL=models.d.ts.map
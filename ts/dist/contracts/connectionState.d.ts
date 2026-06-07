export declare const ConnectionState: {
    readonly DISCONNECTED: "disconnected";
    readonly CONNECTING: "connecting";
    readonly CONNECTED: "connected";
    readonly DEGRADED: "degraded";
    readonly RECONNECTING: "reconnecting";
    readonly ERROR: "error";
};
export type ConnectionState = typeof ConnectionState[keyof typeof ConnectionState];
//# sourceMappingURL=connectionState.d.ts.map
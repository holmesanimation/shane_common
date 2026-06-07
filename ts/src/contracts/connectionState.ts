export const ConnectionState = {
  DISCONNECTED:  'disconnected',
  CONNECTING:    'connecting',
  CONNECTED:     'connected',
  DEGRADED:      'degraded',
  RECONNECTING:  'reconnecting',
  ERROR:         'error',
} as const;
export type ConnectionState = typeof ConnectionState[keyof typeof ConnectionState];

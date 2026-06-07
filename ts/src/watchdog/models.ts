export const AppLiveness = {
  UNKNOWN:          'UNKNOWN',
  STARTING:         'STARTING',
  HEALTHY:          'HEALTHY',
  STALE:            'STALE',
  DEAD:             'DEAD',
  EXPECTED_EXIT:    'EXPECTED_EXIT',
  UNEXPECTED_EXIT:  'UNEXPECTED_EXIT',
} as const;
export type AppLiveness = typeof AppLiveness[keyof typeof AppLiveness];

export interface HeartbeatRecord {
  app_id: string;
  pid: number;
  seq: number;
  ts: number;
  app_state: string;
  schema_version: number;
}

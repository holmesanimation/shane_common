export interface HelloRequest {
  op: 'hello';
  client_name: string;
  client_version: string;
  protocol_version: number;
}

export interface HelloAckPayload {
  op: 'hello.ack';
  session_id: string;
  server_name: string;
  platform_version: string;
  protocol_version: number;
  ts: number;
}

export interface HeartbeatMsg {
  op: 'heartbeat';
  ts: number;
}

export interface PingMsg {
  op: 'ping';
  nonce: string;
}

export interface PongMsg {
  op: 'pong';
  nonce: string;
  ts: number;
}

export interface SubscribeMsg {
  op: 'subscribe';
  topic: string;
  filters?: Record<string, string>;
  last_seq?: number;
}

export interface UnsubscribeMsg {
  op: 'unsubscribe';
  topic: string;
}

export interface CommandMsg {
  op: 'command';
  command_id: string;
  command: string;
  params: Record<string, unknown>;
}

export interface LogWriteParams {
  level: 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';
  message: string;
  source: string;
  ts: number;
}

export interface WatchdogHeartbeatParams {
  app_id: string;
  pid: number;
  app_state: string;
  seq: number;
  ts: number;
}

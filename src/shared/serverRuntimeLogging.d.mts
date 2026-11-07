export interface RuntimeLogger {
  debug(...values: unknown[]): void;
  info(...values: unknown[]): void;
  warning(...values: unknown[]): void;
  error(...values: unknown[]): void;
  critical(...values: unknown[]): void;
  withContext(context: Record<string, unknown>): RuntimeLogger;
}
export interface RuntimeLogSink {
  path: string;
  owner_id: string;
  role: string;
  source: string;
  device: string;
  inode: string;
}
export interface RuntimeLoggingStatus {
  service: string;
  pid: number;
  runtime_instance_id: string;
  applied_revision: string | null;
  level: string;
  pretty: boolean;
  log_sink: RuntimeLogSink | null;
}

export function createRuntimeLogger(service: string, logger: string): RuntimeLogger;
export function runtimeLoggingState(service: string): RuntimeLoggingStatus;

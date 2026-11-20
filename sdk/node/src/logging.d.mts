export interface RuntimeLogger {
  debug(...values: unknown[]): void;
  info(...values: unknown[]): void;
  warning(...values: unknown[]): void;
  error(...values: unknown[]): void;
  critical(...values: unknown[]): void;
  withContext(context: Record<string, unknown>): RuntimeLogger;
}
export interface RuntimeLogRecord {
  timestamp: string;
  level: string;
  service: string;
  logger: string;
  pid: number;
  runtime_instance_id: string;
  message: string;
  context?: Record<string, unknown>;
  exception?: string;
}
export interface RuntimeLogContract {
  readonly version: number;
  readonly root: readonly string[];
  readonly paths: Record<string, readonly string[]>;
  readonly rotation: { readonly max_bytes: number; readonly archives: number };
  readonly retention: { readonly inactive: { readonly max_bytes: number } };
  readonly record: {
    readonly required_fields: readonly string[];
    readonly context_fields: readonly string[];
    readonly levels: Record<string, number>;
    readonly defaults: { readonly level: string; readonly pretty: boolean };
    readonly pretty: string;
    readonly compact: string;
    readonly timestamp: string;
    readonly raw_output: string;
  };
  fileIdentity(descriptor: number): { device: string; inode: string };
  sinkMatches(sink: RuntimeLogSink, descriptors: number[]): boolean;
  rootFor(home: string): string;
  pathFor(root: string, role: string, instance?: string | null): string;
}
export interface RuntimeLogWriter {
  sink: RuntimeLogSink | null;
  descriptors: number[];
}
export interface RuntimeLoggingUpdate {
  runtime_instance_id: string;
  revision: string;
  expected_revision: string | null;
  level: string;
  pretty: boolean;
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

export class RuntimeLoggingConflictError extends Error { statusCode: 409; }
export function registerRuntimeLogSink(writer: RuntimeLogWriter): void;
export function loadRuntimeLogContract(): RuntimeLogContract;
export function configureRuntimeLogging(settings?: { level?: string; pretty?: boolean }, contract?: RuntimeLogContract): {
  level: string; pretty: boolean; pid: number; runtime_instance_id: string;
};
export function createRuntimeLogger(service: string, logger: string, stream?: Pick<NodeJS.WritableStream, 'write'> | null,
  contract?: RuntimeLogContract | null): RuntimeLogger;
export function runtimeLoggingState(service: string, contract?: RuntimeLogContract): RuntimeLoggingStatus;
export function applyRuntimeLogging(payload: RuntimeLoggingUpdate, service: string, contract?: RuntimeLogContract,
  logger?: RuntimeLogger | null): RuntimeLoggingStatus;
export function formatRuntimeLogRecord(record: RuntimeLogRecord, pretty: boolean,
  contract?: RuntimeLogContract): string;

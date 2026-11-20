export interface MaintenanceTask {
  id: string;
  interval_ms: number;
  run_immediately: boolean;
  run(signal: AbortSignal): Promise<void>;
}

export function startMaintenance(options: {
  tasks: readonly MaintenanceTask[];
  identity: unknown;
  reportError(taskId: string, identity: unknown, durationMs: number, error: unknown): void | Promise<void>;
}): { close(): Promise<void> };

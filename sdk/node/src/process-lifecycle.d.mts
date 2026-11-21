export interface ProcessStat {
  pid: number;
  state?: string;
  ppid: number;
  processGroupId: number;
  processSessionId: number | string;
  processStartTime: number | string;
}

export function parseLinuxProcStat(raw: string): ProcessStat;
export function parsePosixPsStat(raw: string, options?: { numericSession?: boolean }): ProcessStat | null;
export function parsePosixSessionProcesses(raw: string, processSessionId: number | string): number[];
export function readProcessStat(pid: number): Promise<ProcessStat | null>;
export function processExists(pid: number): Promise<boolean>;
export function terminateProcessTree(identity: {
  pid: number;
  processSessionId?: number | string | null;
  processStartTime?: number | string | null;
}): Promise<"terminated" | "force_killed" | "still_running" | "not_found">;

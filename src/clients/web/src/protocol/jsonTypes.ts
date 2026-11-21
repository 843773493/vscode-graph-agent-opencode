export interface SseErrorDTO {
  message: string;
}

export interface TraceEventDTO {
  event_id: string;
  part_id?: string | null;
  session_id: string;
  thread_id: string;
  job_id: string;
  type: string;
  phase: string;
  title: string;
  content: string;
  status?: string | null;
  tool_name?: string | null;
  skill_names?: string[];
  step_id?: string | null;
  timestamp: string;
  raw?: Record<string, unknown>;
}

export type WorkspaceFileChangeBatchDTO = import("../types/protocol").WorkspaceFileStreamBatch;

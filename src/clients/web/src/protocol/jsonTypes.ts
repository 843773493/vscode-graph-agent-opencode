export interface SseErrorDTO {
  message: string;
}

export interface SessionExecutionSseDTO {
  event: {
    event_id: string;
    session_id: string;
    job_id?: string | null;
    time: string;
    type: string;
    payload: Record<string, unknown>;
  };
  raw_type: string;
  raw_payload?: Record<string, unknown>;
}

export interface TraceEventDTO {
  event_id: string;
  part_id?: string | null;
  session_id: string;
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

export interface WorkspaceFileChangeBatchDTO {
  changes: Array<{
    kind: "create" | "edit" | "delete";
    path: string;
  }>;
  overflow: boolean;
}

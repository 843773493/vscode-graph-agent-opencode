// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export type JobStatus =
  | "accepted"
  | "queued"
  | "running"
  | "streaming"
  | "waiting_input"
  | "paused"
  | "interrupt_pending"
  | "cancelling"
  | "completed"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "timed_out";

export interface SessionExecutionSseDTO {
  event:
    | MessageUpdatedExecutionEventDTO
    | MessageDeltaExecutionEventDTO
    | JobUpdatedExecutionEventDTO
    | JobStepUpdatedExecutionEventDTO
    | JobStatusChangedExecutionEventDTO
    | SessionStatusChangedExecutionEventDTO
    | SessionCompletedExecutionEventDTO
    | SessionErrorExecutionEventDTO
    | TraceObservedExecutionEventDTO;
  raw_type: string;
  raw_payload?: {
    [k: string]: unknown;
  };
}
export interface MessageUpdatedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "message.updated";
  payload: MessageObservationDTO;
  [k: string]: unknown;
}
export interface MessageObservationDTO {
  message_id: string;
  session_id: string;
  role: string;
  content: string;
  attachments?: unknown[];
  metadata?: {
    [k: string]: unknown;
  };
  created_at: string;
  [k: string]: unknown;
}
export interface MessageDeltaExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "message.delta";
  payload: MessageDeltaDTO;
  [k: string]: unknown;
}
export interface MessageDeltaDTO {
  message_id: string;
  part_id?: string | null;
  kind: "text" | "reasoning" | "tool";
  delta: string;
  final?: boolean;
  metadata?: {
    [k: string]: unknown;
  };
  [k: string]: unknown;
}
export interface JobUpdatedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "job.updated";
  payload: JobProgressDTO;
  [k: string]: unknown;
}
export interface JobProgressDTO {
  job_id: string;
  status: JobStatus;
  current_step_id?: string | null;
  progress?: number;
  message?: string | null;
  [k: string]: unknown;
}
export interface JobStepUpdatedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "job.step.updated";
  payload: JobStepProgressDTO;
  [k: string]: unknown;
}
export interface JobStepProgressDTO {
  agent_id?: string | null;
  message?: string | null;
  phase?: string | null;
  [k: string]: unknown;
}
export interface JobStatusChangedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "job.status.changed";
  payload: JobProgressDTO;
  [k: string]: unknown;
}
export interface SessionStatusChangedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "session.status.changed";
  payload: SessionStatusDTO | SessionObservationStateDTO;
  [k: string]: unknown;
}
export interface SessionStatusDTO {
  session_id: string;
  status: "idle" | "busy" | "question" | "permission" | "retry" | "offline";
  message?: string | null;
  active_job_id?: string | null;
  waiting?: SessionNetworkWaitDTO | null;
  [k: string]: unknown;
}
export interface SessionNetworkWaitDTO {
  id: string;
  session_id: string;
  message: string;
  restored: boolean;
  created_at: string;
  restored_at?: string | null;
  [k: string]: unknown;
}
export interface SessionObservationStateDTO {
  session_id: string;
  active_job_id?: string | null;
  is_streaming?: boolean;
  is_idle?: boolean;
  [k: string]: unknown;
}
export interface SessionCompletedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "session.completed";
  payload: JobProgressDTO;
  [k: string]: unknown;
}
export interface SessionErrorExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "session.error";
  payload: SessionErrorPayloadDTO;
  [k: string]: unknown;
}
export interface SessionErrorPayloadDTO {
  error: string;
  [k: string]: unknown;
}
export interface TraceObservedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "trace.observed";
  payload: TraceObservedPayloadDTO;
  [k: string]: unknown;
}
export interface TraceObservedPayloadDTO {
  raw_type: string;
  [k: string]: unknown;
}
export interface SseErrorDTO {
  message: string;
}
export interface TraceEventDTO {
  event_id: string;
  part_id?: string | null;
  session_id: string;
  job_id: string;
  type:
    | "agent_start"
    | "llm_request"
    | "model_failed"
    | "tool_call_start"
    | "tool_call_end"
    | "agent_end"
    | "error"
    | "job_created"
    | "job_merged"
    | "job_started"
    | "job_completed"
    | "job_cancelled"
    | "job_failed"
    | "status_change"
    | "agent_step"
    | "text_start"
    | "text_delta"
    | "text_end"
    | "message_created"
    | "session_interrupted"
    | "goal_updated"
    | "goal_cleared";
  phase: "agent" | "llm" | "tool" | "error" | "job" | "text" | "system" | "status" | "message" | "session" | "goal";
  title: string;
  content: string;
  status?: string | null;
  tool_name?: string | null;
  skill_names?: string[];
  step_id?: string | null;
  timestamp: string;
  raw?: {
    [k: string]: unknown;
  };
}
export interface WorkspaceFileChangeBatchDTO {
  changes: WorkspaceFileChangeDTO[];
  overflow: boolean;
}
export interface WorkspaceFileChangeDTO {
  kind: "create" | "edit" | "delete";
  path: string;
  [k: string]: unknown;
}

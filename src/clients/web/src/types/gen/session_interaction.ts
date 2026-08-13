// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export type RunMode = "single_agent" | "multi_agent";
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
export type MessageRole = "user" | "assistant" | "system" | "tool";
export type StepStatus = "pending" | "running" | "completed" | "failed" | "skipped" | "cancelled";
export type JobStatus1 =
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

export interface JobDTO {
  created_at: string;
  updated_at: string;
  job_id: string;
  message_id: string;
  session_id: string;
  mode: RunMode;
  status: JobStatus;
  entry_agent: string;
  progress?: number;
  current_step?: string | null;
  error_message?: string | null;
  metadata?: {
    [k: string]: unknown;
  };
  ended_at?: string | null;
}
export interface JobProgressDTO {
  job_id: string;
  status: JobStatus;
  current_step_id?: string | null;
  progress?: number;
  message?: string | null;
}
export interface JobStatusChangedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "job.status.changed";
  payload: JobProgressDTO;
}
export interface JobStepProgressDTO {
  agent_id?: string | null;
  message?: string | null;
  phase?: string | null;
}
export interface JobStepUpdatedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "job.step.updated";
  payload: JobStepProgressDTO;
}
export interface JobUpdatedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "job.updated";
  payload: JobProgressDTO;
}
export interface MessageDTO {
  created_at: string;
  updated_at: string;
  message_id: string;
  session_id: string;
  role: MessageRole;
  content: string;
  attachments?: AttachmentRef[];
  metadata?: {
    [k: string]: unknown;
  };
}
export interface AttachmentRef {
  file_id: string;
  name?: string | null;
  content_type?: string | null;
  data_url?: string | null;
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
}
export interface MessageDeltaExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "message.delta";
  payload: MessageDeltaDTO;
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
}
export interface MessageUpdatedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "message.updated";
  payload: MessageObservationDTO;
}
export interface PermissionRequestDTO {
  id: string;
  session_id: string;
  permission: string;
  patterns?: string[];
  metadata?: {
    [k: string]: unknown;
  };
  always?: string[];
  tool?: {
    [k: string]: string;
  } | null;
}
export interface QuestionInfoDTO {
  question: string;
  header: string;
  options?: QuestionOptionDTO[];
  multiple?: boolean;
  question_key?: string | null;
  header_key?: string | null;
  custom?: boolean;
}
export interface QuestionOptionDTO {
  label: string;
  description: string;
  label_key?: string | null;
  description_key?: string | null;
  mode?: string | null;
}
export interface QuestionRequestDTO {
  id: string;
  session_id: string;
  questions?: QuestionInfoDTO[];
  blocking?: boolean;
  tool?: {
    [k: string]: string;
  } | null;
}
export interface SessionCompletedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "session.completed";
  payload: JobProgressDTO;
}
export interface SessionDTO {
  created_at: string;
  updated_at: string;
  session_id: string;
  workspace_id: string;
  title: string;
  title_source?: "default" | "user" | "auto";
  current_agent_id: string;
  current_provider_id?: string | null;
  parent_session_id?: string | null;
  context_source_session_id?: string | null;
  kind?: "normal" | "context_fork" | "delegated";
  delegation?: SessionDelegationDTO | null;
  generation_origin?: SessionGenerationOriginDTO | null;
}
export interface SessionDelegationDTO {
  parent_session_id: string;
  parent_job_id: string;
  parent_tool_call_id: string;
  subagent_type: string;
  start_status?: "pending" | "running" | "failed";
  start_error?: string | null;
  [k: string]: unknown;
}
/**
 * 会话由通用生成器创建时的不可变来源。
 */
export interface SessionGenerationOriginDTO {
  generator_id: string;
  run_id: string;
  idempotency_key: string;
  generator_type_id: string;
  generator_type_version: string;
  [k: string]: unknown;
}
export interface SessionErrorExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "session.error";
  payload: SessionErrorPayloadDTO;
}
export interface SessionErrorPayloadDTO {
  error: string;
}
export interface SessionExecutionEventBaseDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
}
export interface SessionExecutionSnapshotDTO {
  created_at: string;
  updated_at: string;
  session: SessionDTO;
  message: MessageDTO;
  job?: JobDTO | null;
  steps?: StepDTO[];
  status?: JobStatus1;
  active_step_status?: StepStatus | null;
  last_event_id?: string | null;
}
export interface StepDTO {
  created_at: string;
  updated_at: string;
  step_id: string;
  job_id: string;
  parent_step_id?: string | null;
  agent_id?: string | null;
  step_type: string;
  status: StepStatus;
  input_payload?: {
    [k: string]: unknown;
  };
  output_payload?: {
    [k: string]: unknown;
  };
  started_at?: string | null;
  ended_at?: string | null;
}
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
export interface SessionStatusChangedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "session.status.changed";
  payload: SessionStatusDTO | SessionObservationStateDTO;
}
export interface SessionStatusDTO {
  session_id: string;
  status: "idle" | "busy" | "question" | "permission" | "retry" | "offline";
  message?: string | null;
  active_job_id?: string | null;
  waiting?: SessionNetworkWaitDTO | null;
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
}
export interface TraceObservedExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  time: string;
  type: "trace.observed";
  payload: TraceObservedPayloadDTO;
}
export interface TraceObservedPayloadDTO {
  raw_type: string;
}
export interface TimestampedDTO {
  created_at: string;
  updated_at: string;
}
export type SessionExecutionEventDTO = SessionExecutionSseDTO["event"];

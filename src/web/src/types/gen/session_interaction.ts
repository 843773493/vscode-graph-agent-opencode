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
export interface SessionDTO {
  created_at: string;
  updated_at: string;
  session_id: string;
  workspace_id: string;
  title: string;
  title_source?: "default" | "user" | "auto";
  current_agent_id: string;
  parent_session_id?: string | null;
  kind?: "normal" | "context_fork" | "delegated";
  delegation?: SessionDelegationDTO | null;
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
export interface SessionExecutionEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  type:
    | "message.updated"
    | "message.delta"
    | "job.updated"
    | "job.step.updated"
    | "job.status.changed"
    | "session.status.changed"
    | "session.completed"
    | "session.error";
  time: string;
  payload:
    | MessageDTO
    | MessageDeltaDTO
    | JobDTO
    | StepDTO
    | JobProgressDTO
    | {
        [k: string]: unknown;
      };
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
export interface SessionExecutionSseDTO {
  event: SessionExecutionEventDTO;
  raw_type: string;
  raw_payload?: {
    [k: string]: unknown;
  };
}
export interface TimestampedDTO {
  created_at: string;
  updated_at: string;
}

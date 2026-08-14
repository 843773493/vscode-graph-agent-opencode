// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export type ControlScope = "job" | "agent" | "step";
export type ControlAction =
  | "pause"
  | "resume"
  | "cancel"
  | "skip"
  | "replace_instruction"
  | "append_instruction"
  | "retry";
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
export type RunMode = "single_agent" | "multi_agent";
export type StepStatus = "pending" | "running" | "completed" | "failed" | "skipped" | "cancelled";

export interface JobControlRequest {
  scope?: ControlScope;
  action: ControlAction;
  agent_id?: string | null;
  step_id?: string | null;
  message?: string | null;
  params?: {
    [k: string]: unknown;
  };
}
export interface JobControlResponseDTO {
  job_id: string;
  status: JobStatus;
  control_state: string;
}
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
/**
 * JobService 在调度锁内生成的目标会话队列快照。
 */
export interface JobDispatchSnapshotDTO {
  session_id: string;
  job_id: string;
  job_status: "queued" | "running";
  active_job_id: string;
  blocked_by_job_id?: string | null;
  queued_jobs_ahead: number;
  queued_job_count: number;
  pending_job_count: number;
  pending_kind?: ("queued" | "steering") | null;
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
export interface TimestampedDTO {
  created_at: string;
  updated_at: string;
}

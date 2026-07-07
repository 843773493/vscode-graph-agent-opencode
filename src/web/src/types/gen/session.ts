// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface DeleteSessionResultDTO {
  session_id: string;
  status: string;
  cleaned_jobs?: number;
  cleaned_background_tasks?: number;
  cleaned_terminals?: number;
}
export interface SessionAutoContinueStartRequest {
  poll_interval_seconds?: number;
}
export interface SessionAutoContinueStatusDTO {
  session_id: string;
  enabled: boolean;
  task_id: string | null;
  task_status: string;
  poll_interval_seconds: number | null;
  started_at: string | null;
  forwarded_count: number;
  last_forwarded_at: string | null;
  last_trigger_event_id: string | null;
  last_trigger_job_id: string | null;
  last_enqueued_job_id: string | null;
}
export interface SessionCompactResultDTO {
  session_id: string;
  status: "compacted" | "skipped";
  message: string;
  before_message_count: number;
  effective_message_count_before: number;
  effective_message_count_after: number;
  summarized_message_count: number;
  retained_message_count: number;
  summary?: string | null;
  history_file_path?: string | null;
  compacted_at?: string;
}
export interface SessionControlResultDTO {
  session_id: string;
  action: string;
  status: string;
}
export interface SessionCreateRequest {
  title?: string | null;
  agent_id?: string | null;
  title_source?: ("default" | "user" | "auto") | null;
}
export interface SessionDTO {
  created_at: string;
  updated_at: string;
  session_id: string;
  workspace_id: string;
  title: string;
  title_source?: "default" | "user" | "auto";
  current_agent_id: string;
}
export interface SessionInterruptResultDTO {
  session_id: string;
  job_id: string;
  status: string;
  phase: string;
  tool_name?: string | null;
  interrupted_at?: string;
}
export interface SessionListResultDTO {
  items: SessionDTO[];
  total: number;
  cursor?: string | null;
}
export interface SessionUpdateRequest {
  title?: string | null;
  agent_id?: string | null;
  title_source?: ("default" | "user" | "auto") | null;
}
export interface TimestampedDTO {
  created_at: string;
  updated_at: string;
}

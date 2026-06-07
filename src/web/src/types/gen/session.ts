/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface DeleteSessionResultDTO {
  session_id: string;
  status: string;
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
export interface SessionControlResultDTO {
  session_id: string;
  action: string;
  status: string;
}
export interface SessionCreateRequest {
  title?: string | null;
  agent_id?: string | null;
}
export interface SessionDTO {
  created_at: string;
  updated_at: string;
  session_id: string;
  workspace_id: string;
  title: string;
  current_agent_id: string;
}
export interface SessionListResultDTO {
  items: SessionDTO[];
  total: number;
  cursor?: string | null;
}
export interface SessionUpdateRequest {
  title?: string | null;
  agent_id?: string | null;
}
export interface TimestampedDTO {
  created_at: string;
  updated_at: string;
}

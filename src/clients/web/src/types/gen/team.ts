// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface TeamBoardDTO {
  created_at: string;
  updated_at: string;
  team_id: string;
  name: string;
  coordinator_session_id: string;
  version: number;
  members?: TeamMemberDTO[];
  tasks?: TeamTaskDTO[];
  recent_events?: TeamEventDTO[];
}
export interface TeamMemberDTO {
  session_id: string;
  role: string;
  source: "coordinator" | "delegated" | "attached";
  work_mode: "write" | "read_only";
  instructions?: string;
  status?: "active" | "activation_failed" | "removed";
  activation_job_id?: string | null;
  activation_error?: string | null;
  joined_at: string;
  updated_at: string;
}
export interface TeamTaskDTO {
  created_at: string;
  updated_at: string;
  task_id: string;
  title: string;
  description: string;
  phase: "development" | "review" | "test" | "fix" | "other";
  cycle: number;
  assignee_session_id: string;
  status: "queued" | "in_progress" | "blocked" | "completed" | "failed" | "cancelled";
  depends_on_task_ids?: string[];
  assigned_job_id?: string | null;
  summary?: string | null;
  error?: string | null;
  updated_by_session_id: string;
}
export interface TeamEventDTO {
  event_id: string;
  team_id: string;
  type: string;
  actor_session_id: string;
  created_at: string;
  payload?: {
    [k: string]: unknown;
  };
}
export interface TeamListDTO {
  items: TeamBoardDTO[];
}
export interface TeamMemberOperationDTO {
  board: TeamBoardDTO;
  member: TeamMemberDTO;
  child_session_id?: string | null;
  child_message_id?: string | null;
  child_job_id?: string | null;
}
export interface TeamTaskOperationDTO {
  board: TeamBoardDTO;
  task: TeamTaskDTO;
  dispatched_job_id?: string | null;
}
export interface TimestampedDTO {
  created_at: string;
  updated_at: string;
}

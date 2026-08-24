// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface AttachmentRef {
  file_id: string;
  name?: string | null;
  content_type?: string | null;
  data_url?: string | null;
}
/**
 * 会话 FIFO 队列中的单条用户消息。
 */
export interface PendingRequestDTO {
  job_id: string;
  message_id: string;
  session_id: string;
  content: string;
  attachments?: AttachmentRef[];
  delivery_policy: "after_turn" | "after_tool_result" | "after_interrupt";
  enqueue_sequence: number;
  position: number;
  status?: "queued";
  waiting_reason?: string | null;
  last_boundary?: ("idle" | "after_turn" | "after_tool_result" | "after_interrupt") | null;
  agent_id: string;
  message_created_at: string;
  message_metadata?: {
    [k: string]: unknown;
  };
  created_at: string;
  updated_at: string;
  snapshot_version: number;
}
export interface PendingRequestListDTO {
  session_id: string;
  active_job_id?: string | null;
  requests?: PendingRequestDTO[];
  snapshot_version?: number;
}
export interface PendingRequestPolicyUpdateRequest {
  delivery_policy: "after_turn" | "after_tool_result" | "after_interrupt";
  expected_snapshot_version?: number | null;
}
export interface PendingRequestSummaryDTO {
  job_id: string;
  message_id: string;
  enqueue_sequence: number;
  delivery_policy: "after_turn" | "after_tool_result" | "after_interrupt";
  status: "queued";
  updated_at: string;
}
export interface PendingRequestSummaryListDTO {
  session_id: string;
  active_job_id?: string | null;
  requests?: PendingRequestSummaryDTO[];
  request_count: number;
  snapshot_version?: number;
  truncated?: boolean;
}
export interface PendingRequestUpdateRequest {
  content: string;
  attachments?: AttachmentRef[];
}

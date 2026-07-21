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
 * 会话中尚未开始执行的用户请求。
 */
export interface PendingRequestDTO {
  job_id: string;
  message_id: string;
  session_id: string;
  content: string;
  attachments?: AttachmentRef[];
  kind: "queued" | "steering";
  position: number;
  agent_id: string;
  message_created_at: string;
  message_metadata?: {
    [k: string]: unknown;
  };
  created_at: string;
  updated_at: string;
}
export interface PendingRequestListDTO {
  session_id: string;
  active_job_id?: string | null;
  yield_requested?: boolean;
  requests?: PendingRequestDTO[];
}
export interface PendingRequestOrderItem {
  message_id: string;
  kind: "queued" | "steering";
}
export interface PendingRequestReorderRequest {
  requests: PendingRequestOrderItem[];
}
export interface PendingRequestUpdateRequest {
  content: string;
  attachments?: AttachmentRef[];
}

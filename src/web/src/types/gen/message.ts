// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export type MessageRole = "user" | "assistant" | "system" | "tool";
export type MessageRole1 = "user" | "assistant" | "system" | "tool";
export type RunMode = "single_agent" | "multi_agent";

export interface AgentStateMessagesDTO {
  session_id: string;
  message_count: number;
  jsonl: string;
}
export interface AttachmentRef {
  file_id: string;
  name?: string | null;
  content_type?: string | null;
  data_url?: string | null;
}
export interface MessageCreateRequest {
  role?: MessageRole;
  content: string;
  attachments?: AttachmentRef[];
  metadata?: {
    [k: string]: unknown;
  };
}
export interface MessageDTO {
  created_at: string;
  updated_at: string;
  message_id: string;
  session_id: string;
  role: MessageRole1;
  content: string;
  attachments?: AttachmentRef[];
  metadata?: {
    [k: string]: unknown;
  };
}
export interface MessageReplayAccepted {
  message_id: string;
  job_id: string;
  status: string;
  session_id: string;
  action: "retry_failed" | "regenerate" | "edit_and_continue";
  replaced_message_id: string;
  removed_message_count: number;
  workspace_changes_reverted?: false;
  notice: string;
}
export interface MessageReplayRequest {
  action: "retry_failed" | "regenerate" | "edit_and_continue";
  content?: string | null;
  acknowledge_context_only?: boolean;
}
export interface MessageRunAccepted {
  message_id: string;
  job_id: string;
  status: string;
}
export interface MessageRunRequest {
  message: MessageCreateRequest;
  run: RunOptions;
}
export interface RunOptions {
  mode?: RunMode;
  agent_id?: string | null;
  response_mode?: string;
  async?: boolean;
  max_steps?: number;
  timeout_seconds?: number;
  context?: {
    [k: string]: unknown;
  };
}
export interface TimestampedDTO {
  created_at: string;
  updated_at: string;
}

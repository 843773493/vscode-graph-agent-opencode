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
export interface SessionTurnBootstrapDTO {
  session: SessionDTO;
  latest_turn?: TurnSummaryDTO | null;
  active_job_id?: string | null;
  /**
   * @maxItems 8
   */
  active_jobs?:
    | []
    | [TurnJobSummaryDTO]
    | [TurnJobSummaryDTO, TurnJobSummaryDTO]
    | [TurnJobSummaryDTO, TurnJobSummaryDTO, TurnJobSummaryDTO]
    | [TurnJobSummaryDTO, TurnJobSummaryDTO, TurnJobSummaryDTO, TurnJobSummaryDTO]
    | [TurnJobSummaryDTO, TurnJobSummaryDTO, TurnJobSummaryDTO, TurnJobSummaryDTO, TurnJobSummaryDTO]
    | [TurnJobSummaryDTO, TurnJobSummaryDTO, TurnJobSummaryDTO, TurnJobSummaryDTO, TurnJobSummaryDTO, TurnJobSummaryDTO]
    | [
        TurnJobSummaryDTO,
        TurnJobSummaryDTO,
        TurnJobSummaryDTO,
        TurnJobSummaryDTO,
        TurnJobSummaryDTO,
        TurnJobSummaryDTO,
        TurnJobSummaryDTO
      ]
    | [
        TurnJobSummaryDTO,
        TurnJobSummaryDTO,
        TurnJobSummaryDTO,
        TurnJobSummaryDTO,
        TurnJobSummaryDTO,
        TurnJobSummaryDTO,
        TurnJobSummaryDTO,
        TurnJobSummaryDTO
      ];
  active_job_count?: number;
  active_jobs_truncated?: boolean;
  projection_state?: "ready" | "partial";
  older_cursor?: string | null;
  event_cursor?: string | null;
  projection_epoch: number;
}
export interface TurnSummaryDTO {
  turn_id: string;
  job_id: string;
  session_id: string;
  ordinal: number;
  revision: number;
  status: JobStatus;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  items_view?: "summary";
  /**
   * @maxItems 32
   */
  source_message_ids?: string[];
  source_message_count?: number;
  /**
   * @maxItems 32
   */
  merged_job_ids?: string[];
  merged_job_count?: number;
  sources_truncated?: boolean;
  /**
   * @maxItems 8
   */
  user_messages?:
    | []
    | [TurnUserMessageSummaryDTO]
    | [TurnUserMessageSummaryDTO, TurnUserMessageSummaryDTO]
    | [TurnUserMessageSummaryDTO, TurnUserMessageSummaryDTO, TurnUserMessageSummaryDTO]
    | [TurnUserMessageSummaryDTO, TurnUserMessageSummaryDTO, TurnUserMessageSummaryDTO, TurnUserMessageSummaryDTO]
    | [
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO
      ]
    | [
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO
      ]
    | [
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO
      ]
    | [
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO,
        TurnUserMessageSummaryDTO
      ];
  user_message_count?: number;
  user_messages_truncated?: boolean;
  response_preview?: string;
  preview_truncated?: boolean;
  item_count?: number;
}
export interface TurnUserMessageSummaryDTO {
  message_id: string;
  preview?: string;
  content_truncated?: boolean;
  attachment_count?: number;
  created_at: string;
}
export interface TurnJobSummaryDTO {
  job_id: string;
  message_id: string;
  status: JobStatus;
  updated_at: string;
}
export interface StaleTurnCursorErrorDTO {
  code?: "stale_turn_cursor";
  session_id: string;
  cursor_epoch: number;
  current_epoch: number;
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
/**
 * Turn 展示层只保存持久化附件引用，不携带内联 data URL。
 */
export interface TurnAttachmentDTO {
  file_id: string;
  name?: string | null;
  content_type?: string | null;
}
/**
 * Turn summary 与 detail 共享的稳定身份和状态。
 */
export interface TurnBaseDTO {
  turn_id: string;
  job_id: string;
  session_id: string;
  ordinal: number;
  revision: number;
  status: JobStatus;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
}
/**
 * 服务端编码进不透明 cursor 的稳定锚点。
 */
export interface TurnCursorDTO {
  version?: 1;
  session_id: string;
  projection_epoch: number;
  anchor_turn_id: string;
  include_anchor?: boolean;
  direction?: "older";
}
export interface TurnDetailBatchDTO {
  /**
   * @maxItems 4
   */
  items:
    | []
    | [TurnDetailDTO]
    | [TurnDetailDTO, TurnDetailDTO]
    | [TurnDetailDTO, TurnDetailDTO, TurnDetailDTO]
    | [TurnDetailDTO, TurnDetailDTO, TurnDetailDTO, TurnDetailDTO];
  projection_epoch: number;
}
export interface TurnDetailDTO {
  turn_id: string;
  job_id: string;
  session_id: string;
  ordinal: number;
  revision: number;
  status: JobStatus;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  items_view?: "full";
  source_message_ids?: string[];
  merged_job_ids?: string[];
  user_messages?: TurnUserMessageDTO[];
  response_preview?: string;
  preview_truncated?: boolean;
  final_response?: string;
  items?: TraceEventDTO[];
}
/**
 * 一次执行 Turn 所消费的用户可见消息。
 */
export interface TurnUserMessageDTO {
  message_id: string;
  content: string;
  attachments?: TurnAttachmentDTO[];
  metadata?: {
    [k: string]: unknown;
  };
  created_at: string;
}
export interface TurnDetailBatchRequest {
  /**
   * @minItems 1
   * @maxItems 4
   */
  turn_ids: [string] | [string, string] | [string, string, string] | [string, string, string, string];
}
export interface TurnPageDTO {
  /**
   * @maxItems 20
   */
  items:
    | []
    | [TurnSummaryDTO]
    | [TurnSummaryDTO, TurnSummaryDTO]
    | [TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO]
    | [TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO]
    | [TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO]
    | [TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO]
    | [TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO, TurnSummaryDTO]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ]
    | [
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO,
        TurnSummaryDTO
      ];
  next_cursor?: string | null;
  has_more?: boolean;
  projection_epoch: number;
}
export interface TurnProjectionCorruptedErrorDTO {
  code?: "turn_projection_corrupted";
  session_id: string;
  message: string;
}

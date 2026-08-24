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
  /**
   * @maxItems 32
   */
  thinking_blocks?: TurnThinkingBlockDTO[];
  /**
   * @maxItems 64
   */
  tool_summary?: TurnToolSummaryDTO[];
  /**
   * @maxItems 128
   */
  response_parts?: TurnResponsePartDTO[];
  activity_stats?: TurnActivityStatsDTO;
}
export interface TurnUserMessageSummaryDTO {
  message_id: string;
  preview?: string;
  content_truncated?: boolean;
  attachment_count?: number;
  created_at: string;
}
/**
 * 安全的思考投影；encrypted 块只表达存在性，不携带 provider 密文。
 */
export interface TurnThinkingBlockDTO {
  kind: "reasoning" | "summary" | "encrypted";
  text?: string;
}
export interface TurnToolSummaryDTO {
  tool_name: string;
  status: string;
  tool_call_id?: string | null;
}
/**
 * 历史和 live 共用的响应部件语义模型。
 */
export interface TurnResponsePartDTO {
  part_id: string;
  kind: "text" | "reasoning" | "reasoning_summary" | "reasoning_encrypted" | "tool_call" | "tool_result" | "final_text";
  projection: "summary" | "detail" | "streaming";
  status?: "pending" | "running" | "completed" | "failed" | "cancelled";
  source: TurnResponseSourceDTO;
  text?: string;
  carrier_type?: string | null;
  tool_call_id?: string | null;
  tool_name?: string | null;
  arguments?: string | null;
  result?: string | null;
  truncated?: boolean;
  final?: boolean;
}
/**
 * 响应部件在 canonical rollout 中的稳定来源坐标。
 *
 * ``assistant_message_sequence`` 只用于把 ToolMessage 结果关联回产生它的
 * assistant；它不是新的全局 part 序号。``call_index`` 仍然只表示同一
 * assistant 的 tool_calls 列表顺序。
 */
export interface TurnResponseSourceDTO {
  message_sequence: number;
  assistant_message_sequence?: number | null;
  content_block_index?: number | null;
  item_index?: number | null;
  call_index?: number | null;
  result_message_sequence?: number | null;
}
/**
 * Turn 折叠行使用的轻量 rollout message 统计，不包含消息正文。
 */
export interface TurnActivityStatsDTO {
  duration_ms?: number | null;
  message_count?: number;
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
/**
 * 请求的 Turn 仍存在于 rollout，但不再属于当前 context view。
 */
export interface StaleTurnReferenceErrorDTO {
  code?: "stale_turn_reference";
  session_id: string;
  /**
   * @minItems 1
   * @maxItems 4
   */
  turn_ids: [string] | [string, string] | [string, string, string] | [string, string, string, string];
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
  direction?: "head" | "tail" | "before" | "after" | "around" | "older";
  stage?: number;
}
/**
 * 内部 Turn 投影仓储使用的详情批次类型。
 */
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
  next_cursor?: string | null;
  has_more?: boolean;
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
  /**
   * @maxItems 32
   */
  assistant_text?: string[];
  /**
   * @maxItems 32
   */
  thinking_blocks?: TurnThinkingBlockDTO[];
  /**
   * @maxItems 64
   */
  tool_summary?: TurnToolSummaryDTO[];
  /**
   * @maxItems 512
   */
  response_parts?: TurnResponsePartDTO[];
  final_response?: string;
  items?: TraceEventDTO[];
  detail_truncated?: boolean;
  detail_next_cursor?: string | null;
  activity_stats?: TurnActivityStatsDTO;
}
/**
 * 一次执行 Turn 所消费的用户可见消息。
 */
export interface TurnUserMessageDTO {
  message_id: string;
  content: string;
  content_truncated?: boolean;
  attachments?: TurnAttachmentDTO[];
  metadata?: {
    [k: string]: unknown;
  };
  created_at: string;
}
/**
 * 内部 Turn 投影仓储使用的详情批次类型；HTTP 入口统一使用 history。
 */
export interface TurnDetailBatchRequest {
  /**
   * @minItems 1
   * @maxItems 4
   */
  turn_ids: [string] | [string, string] | [string, string, string] | [string, string, string, string];
  include?:
    | []
    | [
        | "user"
        | "text"
        | "assistant_text"
        | "assistant"
        | "thinking"
        | "reasoning_summary"
        | "reasoning_detail"
        | "encrypted_reasoning_meta"
        | "tool_summary"
        | "tool_call"
        | "tool_result"
        | "internal"
        | "metadata"
        | "final_response"
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | null;
}
/**
 * 详情 payload 的不透明续读游标。
 */
export interface TurnDetailCursorDTO {
  version?: 1;
  session_id: string;
  projection_epoch: number;
  turn_id: string;
  event_index: number;
  include_hash: string;
}
/**
 * 语义化历史读取请求；客户端不能通过它绕过服务端硬上限。
 */
export interface TurnHistoryLoadRequest {
  direction?: "head" | "tail" | "before" | "after" | "around" | "older";
  cursor?: string | null;
  anchor_turn_id?: string | null;
  turn_ids?: [string] | [string, string] | [string, string, string] | [string, string, string, string] | null;
  turns?: number | null;
  before_turns?: number | null;
  after_turns?: number | null;
  include?:
    | []
    | [
        | "user"
        | "text"
        | "assistant_text"
        | "assistant"
        | "thinking"
        | "reasoning_summary"
        | "reasoning_detail"
        | "encrypted_reasoning_meta"
        | "tool_summary"
        | "tool_call"
        | "tool_result"
        | "internal"
        | "metadata"
        | "final_response"
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | [
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        ),
        (
          | "user"
          | "text"
          | "assistant_text"
          | "assistant"
          | "thinking"
          | "reasoning_summary"
          | "reasoning_detail"
          | "encrypted_reasoning_meta"
          | "tool_summary"
          | "tool_call"
          | "tool_result"
          | "internal"
          | "metadata"
          | "final_response"
        )
      ]
    | null;
}
export interface TurnHistoryPageDTO {
  /**
   * @maxItems 256
   */
  items: TurnDetailDTO[];
  next_cursor?: string | null;
  has_more?: boolean;
  before_cursor?: string | null;
  after_cursor?: string | null;
  has_before?: boolean;
  has_after?: boolean;
  projection_epoch: number;
}
/**
 * 内部 Turn 投影仓储使用的 summary 页面。
 */
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

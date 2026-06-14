// 该文件由程序生成，请勿手写。

export type * from './gen';

// 补充后端事件流中尚未被生成类型覆盖的事件类型。
// TODO: 待 pydantic2ts 脚本更新后，优先从 OpenAPI 自动生成这些类型。

export type TraceEventType =
  | 'message_created'
  | 'job_created'
  | 'job_started'
  | 'job_completed'
  | 'job_cancelled'
  | 'job_failed'
  | 'status_change'
  | 'agent_start'
  | 'agent_step'
  | 'agent_end'
  | 'error'
  | 'llm_request'
  | 'tool_call_start'
  | 'tool_call_end'
  | 'text_start'
  | 'text_delta'
  | 'text_end'
  | 'system_reminder_injected';

export interface BaseTraceEvent {
  event_id: string;
  job_id: string;
  step_id: string | null;
  agent_id: string | null;
  timestamp: string;
  type: TraceEventType;
  payload: Record<string, unknown>;
}

export type TraceEvent = BaseTraceEvent;

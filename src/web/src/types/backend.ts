// 该文件是前端业务类型适配层，封装后端实际返回结构。
// 由于 src/types/gen/ 中自动生成的类型存在重复导出且部分已过期，
// 本目录业务代码统一从这里导入类型；不直接依赖 gen/index.ts 的通配导出。

export type { AgentDTO as Agent } from "./gen/agent";

export interface APIResponse<T> {
  code: number;
  message: string;
  data: T | null;
  request_id?: string | null;
}

export interface CursorPage<T> {
  items: T[];
  next_cursor?: string | null;
  has_more?: boolean;
}

export interface WorkspaceInfo {
  workspace_id: string;
  root_path: string;
  name: string;
}

export interface Session {
  session_id: string;
  workspace_id: string;
  title: string;
  current_agent_id: string;
  created_at: string;
  updated_at: string;
}

export interface Message {
  message_id: string;
  session_id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  attachments?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface AgentStateMessages {
  session_id: string;
  message_count: number;
  jsonl: string;
}

export interface RunOptions {
  mode?: "single_agent" | "multi_agent";
  agent_id?: string | null;
  response_mode?: string;
  async?: boolean;
  max_steps?: number;
  timeout_seconds?: number;
  context?: Record<string, unknown>;
}

export interface MessageRunRequest {
  message: {
    role?: Message["role"];
    content: string;
    attachments?: Array<Record<string, unknown>>;
    metadata?: Record<string, unknown>;
  };
  run: RunOptions;
}

export interface MessageRunAccepted {
  message_id: string;
  job_id: string;
  status: string;
}

export interface InterruptSessionResult {
  session_id: string;
  job_id: string;
  status: string;
  phase: string;
  tool_name?: string | null;
  interrupted_at: string;
}

export type KnownTraceEventType =
  | "message_created"
  | "job_created"
  | "job_started"
  | "job_completed"
  | "job_cancelled"
  | "job_failed"
  | "status_change"
  | "agent_start"
  | "agent_step"
  | "agent_end"
  | "error"
  | "llm_request"
  | "tool_call_start"
  | "tool_call_end"
  | "text_start"
  | "text_delta"
  | "text_end"
  | "session_interrupted";

export type TraceEventType = KnownTraceEventType | (string & {});

export interface BaseTraceEvent {
  event_id: string;
  session_id?: string;
  job_id: string;
  step_id: string | null;
  agent_id: string | null;
  timestamp: string;
  type: TraceEventType;
  payload?: Record<string, unknown>;
  phase?: string;
  title?: string;
  content?: string;
  status?: string | null;
  tool_name?: string | null;
  /** 后端 DTO 格式可能将真实事件数据嵌套在 raw 中 */
  raw?: {
    event_id: string;
    job_id: string;
    type: string;
    timestamp: string;
    payload: Record<string, unknown>;
    session_id?: string;
    agent_id?: string | null;
    step_id?: string | null;
  };
}

export type TraceEvent = BaseTraceEvent;

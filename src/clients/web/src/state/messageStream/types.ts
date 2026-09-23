// 消息流协议派生类型：事件信封、实体结构、状态结构与连接状态。
import type { MessageStreamSnapshot } from "../../api/stream/messageStreamSnapshot";

export type MessageStreamEventType =
  | "stream.opened"
  | "model.started"
  | "model.completed"
  | "model.retrying"
  | "model.failed"
  | "block.started"
  | "block.delta"
  | "block.completed"
  | "tool_call"
  | "tool_call.delta"
  | "tool_call.completed"
  | "tool.started"
  | "tool.completed"
  | "activity.started"
  | "activity.updated"
  | "activity.completed"
  | "activity.failed"
  | "interrupt.requested"
  | "interrupt.rejected"
  | "stream.completed"
  | "stream.interrupted"
  | "stream.failed"
  | "stream.snapshot";

interface MessageStreamEventEnvelope {
  event_id: string;
  session_id: string;
  turn_id: string;
  turn_stream_id: string;
  event_seq: number;
  emitted_at?: string;
  workspace_id?: string;
  model_call_id?: string;
  block_id?: string;
  tool_execution_id?: string;
  tool_call_id?: string;
  tool_invocation_id?: string;
  tool_attempt_id?: string;
  job_id?: string;
}

export interface MessageStreamDataEvent extends MessageStreamEventEnvelope {
  type: Exclude<MessageStreamEventType, "stream.snapshot">;
  payload: Record<string, unknown>;
}

export interface MessageStreamSnapshotEvent extends MessageStreamEventEnvelope {
  type: "stream.snapshot";
  payload: MessageStreamSnapshot;
}

export type MessageStreamEvent = MessageStreamDataEvent | MessageStreamSnapshotEvent;

export interface MessageStreamLifecycle {
  started_seq?: number;
  last_event_seq?: number;
  completed_seq?: number;
  started_at?: string;
  updated_at?: string;
  completed_at?: string;
}

export interface MessageStreamBlock extends MessageStreamLifecycle {
  block_id: string;
  model_call_id: string | null;
  block_index: number;
  carrier_type: string;
  status: "running" | "completed" | "failed" | "interrupted";
  text: string;
  items: Record<string, unknown>[];
  redacted: boolean;
  projection: string;
  completion_reason?: string;
  partial?: boolean;
}

export interface MessageStreamToolExecution extends MessageStreamLifecycle {
  tool_execution_id: string;
  tool_call_id: string;
  tool_invocation_id?: string;
  tool_attempt_id?: string;
  tool_name: string;
  status: "running" | "completed" | "failed";
  outcome?: "success" | "provider_error" | "execution_lost" | "outcome_unknown";
  completion_reason?: string;
  result?: string;
  error?: string;
}

export interface MessageStreamActivity extends MessageStreamLifecycle {
  activity_id: string;
  kind: string;
  parent_activity_id?: string;
  scope_ref: string;
  status: "running" | "waiting" | "stopping" | "completed" | "failed" | "unknown";
  outcome?: string;
  summary?: string;
  cancellable: boolean;
  resumable: boolean;
  side_effect_policy: string;
  resource_refs: string[];
  detail?: Record<string, unknown>;
  detail_ref?: string;
  detail_available: boolean;
  detail_error?: string;
  completion_reason?: string;
}

export interface MessageStreamActiveState {
  kind: string;
  phase: string;
  entity_id: string;
  carrier_type?: string;
  block_id?: string;
  tool_call_id?: string;
  tool_invocation_id?: string;
  tool_attempt_id?: string;
  tool_execution_id?: string;
  activity_id?: string;
  activity_kind?: string;
  status: string;
  last_kind?: string;
  last_phase?: string;
  reason?: string;
  detail_ref?: string;
}

export interface MessageStreamState {
  workspaceId: string | null;
  sessionId: string;
  turnId: string;
  turnStreamId: string;
  lastEventSeq: number;
  streamStatus: "open" | "interrupting" | "completed" | "interrupted" | "failed";
  agentLoopStatus: string;
  currentModelCallId: string | null;
  currentAttempt: number;
  blocks: MessageStreamBlock[];
  toolExecutions: MessageStreamToolExecution[];
  toolCalls: Record<string, Record<string, unknown>>;
  interruptState: {
    requestId: string | null;
    status: string;
    reason?: string;
    factConfirmed?: boolean;
  } | null;
  failure: {
    code: string;
    message: string;
    afterInterruptRequested: boolean;
    resumable: boolean;
  } | null;
  activeState: MessageStreamActiveState | null;
  activities: MessageStreamActivity[];
  modelCalls: Record<string, Record<string, unknown>>;
  resourceRefs: Record<string, Record<string, unknown>>;
  recovery: Record<string, unknown> | null;
  pendingEvents: MessageStreamEvent[];
  resumable: boolean;
  connectionStatus:
    | "connecting"
    | "connected"
    | "disconnected"
    | "gap"
    | "terminal"
    | "retry_exhausted";
  protocolError: string | null;
}

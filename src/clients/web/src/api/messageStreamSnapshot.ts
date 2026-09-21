import type {
  Activity,
  ActiveState,
  InterruptState,
  MessageBlockSnapshot,
  ModelCallSnapshot,
  RecoveryState,
  ResourceRef,
  StreamFailure,
  StreamSnapshot,
  ToolCall,
  ToolExecutionSnapshot,
} from "../types/protocol_generated/boxteam/workspace/message/v1/message_stream";
import type { JsonObject } from "@bufbuild/protobuf";

type WireToolExecutionStatus = "running" | "completed" | "failed";
type WireToolExecutionOutcome = "success" | "provider_error" | "execution_lost" | "outcome_unknown";
type WireActivityStatus = "running" | "waiting" | "stopping" | "completed" | "failed" | "unknown";
type WireActivityOutcome = "success" | "user_interrupt" | "provider_error" | "execution_lost" | "outcome_unknown";

type WireBlockSnapshot = Omit<Partial<MessageBlockSnapshot>, "model_call_id" | "block_id"> &
  Pick<MessageBlockSnapshot, "block_id"> & { items: JsonObject[] };
type WireToolExecutionSnapshot = Omit<Partial<ToolExecutionSnapshot>, "tool_execution_id" | "tool_call_id" | "tool_name" | "status" | "outcome"> &
  Pick<ToolExecutionSnapshot, "tool_execution_id" | "tool_call_id" | "tool_name"> & {
    status: WireToolExecutionStatus;
    outcome?: WireToolExecutionOutcome;
  };
type WireToolCall = Omit<Partial<ToolCall>, "tool_call_id" | "tool_name" | "arguments"> &
  Pick<ToolCall, "tool_call_id" | "tool_name"> & { arguments?: JsonObject };
type WireModelCallSnapshot = Omit<Partial<ModelCallSnapshot>, "model_call_id"> &
  Pick<ModelCallSnapshot, "model_call_id">;
type WireResourceRef = Omit<Partial<ResourceRef>, "resource_id"> &
  Pick<ResourceRef, "resource_id">;
type WireActivity = Omit<Partial<Activity>, "activity_id" | "kind" | "status" | "outcome" | "resource_refs" | "detail"> &
  Pick<Activity, "activity_id" | "kind"> & {
    status: WireActivityStatus;
    outcome?: WireActivityOutcome;
    resource_refs: string[];
    detail?: JsonObject;
  };
type WireStreamSnapshot = Omit<Partial<StreamSnapshot>,
  | "snapshot_seq"
  | "stream_status"
  | "agent_loop_status"
  | "current_attempt"
  | "blocks"
  | "tool_executions"
  | "tool_calls"
  | "model_calls"
  | "activities"
  | "resource_refs"
  | "resumable"
  | "interrupt_request_id"
  | "interrupt_status"
> & {
  snapshot_seq: number;
  stream_status: "open" | "interrupting" | "completed" | "interrupted" | "failed";
  agent_loop_status: string;
  current_attempt: number;
  blocks: WireBlockSnapshot[];
  tool_executions: WireToolExecutionSnapshot[];
  tool_calls: WireToolCall[];
  model_calls: WireModelCallSnapshot[];
  activities: WireActivity[];
  resource_refs: WireResourceRef[];
  resumable: boolean;
};

export type MessageStreamSnapshot = WireStreamSnapshot;

export type MessageStreamSnapshotResponse = MessageStreamSnapshot & {
  session_id: string;
  turn_id: string;
  turn_stream_id: string;
  workspace_id?: string;
};

export function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const SNAPSHOT_FIELDS = new Set([
  "workspace_id",
  "snapshot_seq",
  "stream_status",
  "agent_loop_status",
  "current_model_call_id",
  "current_attempt",
  "blocks",
  "tool_executions",
  "failure",
  "resumable",
  "tool_calls",
  "model_calls",
  "activities",
  "resource_refs",
  "active_state",
  "interrupt_state",
  "recovery",
]);

export function validateMessageStreamSnapshot(value: unknown): MessageStreamSnapshotResponse {
  if (!isJsonObject(value)) throw new Error("消息流快照必须是对象");
  for (const field of Object.keys(value)) {
    if (!SNAPSHOT_FIELDS.has(field) && !["session_id", "turn_id", "turn_stream_id"].includes(field)) {
      throw new Error(`消息流快照包含未知字段: ${field}`);
    }
  }
  const requiredStrings = ["session_id", "turn_id", "turn_stream_id"] as const;
  for (const field of requiredStrings) {
    if (typeof value[field] !== "string" || value[field].length === 0) {
      throw new Error(`消息流快照 ${field} 必须是非空字符串`);
    }
  }
  const payload = { ...value };
  delete payload.session_id;
  delete payload.turn_id;
  delete payload.turn_stream_id;
  validateSnapshotPayload(payload, true);
  return value as unknown as MessageStreamSnapshotResponse;
}

export function validateMessageStreamSnapshotPayload(value: unknown): MessageStreamSnapshot {
  if (!isJsonObject(value)) throw new Error("消息流快照必须是对象");
  validateSnapshotPayload(value, false);
  return value as unknown as MessageStreamSnapshot;
}

function validateSnapshotPayload(value: JsonObject, allowIdentityFields: boolean): void {
  for (const field of Object.keys(value)) {
    if (!SNAPSHOT_FIELDS.has(field) && !(allowIdentityFields && ["session_id", "turn_id", "turn_stream_id"].includes(field))) {
      throw new Error(`消息流快照包含未知字段: ${field}`);
    }
  }
  if (typeof value.agent_loop_status !== "string" || value.agent_loop_status.length === 0) {
    throw new Error("消息流快照 agent_loop_status 必须是非空字符串");
  }
  if (!isStreamStatus(value.stream_status)) {
    throw new Error("消息流快照 stream_status 非法");
  }
  if (!isNonNegativeInteger(value.snapshot_seq) || !isNonNegativeInteger(value.current_attempt)) {
    throw new Error("消息流快照序号字段必须是非负整数");
  }
  if (typeof value.resumable !== "boolean") throw new Error("消息流快照 resumable 必须是布尔值");
  for (const field of [
    "blocks",
    "tool_calls",
    "tool_executions",
    "model_calls",
    "activities",
    "resource_refs",
  ] as const) {
    if (!Array.isArray(value[field])) throw new Error(`消息流快照 ${field} 必须是数组`);
  }
  if (value.workspace_id !== undefined && typeof value.workspace_id !== "string") {
    throw new Error("消息流快照 workspace_id 必须是字符串");
  }
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isStreamStatus(value: unknown): value is MessageStreamSnapshotResponse["stream_status"] {
  return value === "open"
    || value === "interrupting"
    || value === "completed"
    || value === "interrupted"
    || value === "failed";
}

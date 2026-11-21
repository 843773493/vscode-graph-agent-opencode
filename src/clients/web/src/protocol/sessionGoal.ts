import { fromJson, type JsonObject } from "@bufbuild/protobuf";
import { SessionGoalDTOSchema, SessionGoalOperationResultDTOSchema, SessionGoalSnapshotDTOSchema } from "../types/protocol_buf_generated/boxteam/workspace/v2/public_pb";
import type { SessionGoal, SessionGoalOperationResultDTO, SessionGoalSnapshotDTO, SessionGoalStatus } from "../types/backend";

const statuses = new Set<SessionGoalStatus>(["active", "paused", "blocked", "usage_limited", "budget_limited", "complete"]);

function record(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error("Goal 协议必须是对象");
  return value as Record<string, unknown>;
}

function version(value: unknown): void {
  if (!Number.isSafeInteger(value) || (value as number) < 0) throw new Error("Goal 协议缺少有效的非负整数版本或用量");
}

export function parseSessionGoal(value: unknown): SessionGoal {
  const goal = record(value);
  fromJson(SessionGoalDTOSchema, goal as JsonObject, { ignoreUnknownFields: false });
  if (typeof goal.goal_id !== "string" || !goal.goal_id || typeof goal.session_id !== "string" || !goal.session_id
    || typeof goal.thread_id !== "string" || !goal.thread_id || typeof goal.objective !== "string" || !goal.objective
    || !statuses.has(goal.status as SessionGoalStatus) || (goal.token_budget !== null
      && (!Number.isSafeInteger(goal.token_budget) || (goal.token_budget as number) <= 0))) {
    throw new Error("Goal 协议缺少有效身份、目标、状态或预算");
  }
  for (const field of ["revision", "control_revision", "sync_version", "tokens_used", "time_used_seconds"]) version(goal[field]);
  if (typeof goal.created_at !== "string" || typeof goal.updated_at !== "string") throw new Error("Goal 协议缺少创建或更新时间");
  return goal as unknown as SessionGoal;
}

function validateSnapshot(value: unknown, sessionId: string): SessionGoalSnapshotDTO {
  const snapshot = record(value);
  if (snapshot.session_id !== sessionId || typeof snapshot.thread_id !== "string" || !snapshot.thread_id) {
    throw new Error("Goal 归属与请求 Session main Thread 不一致");
  }
  version(snapshot.control_revision);
  version(snapshot.sync_version);
  if (snapshot.goal != null) {
    const goal = parseSessionGoal(snapshot.goal);
    if (goal.session_id !== sessionId || goal.thread_id !== snapshot.thread_id
      || goal.control_revision !== snapshot.control_revision || goal.sync_version !== snapshot.sync_version) {
      throw new Error("Goal 对象与快照归属或版本不一致");
    }
  }
  return snapshot as unknown as SessionGoalSnapshotDTO;
}

export function parseSessionGoalSnapshot(value: unknown, sessionId: string): SessionGoalSnapshotDTO {
  fromJson(SessionGoalSnapshotDTOSchema, record(value) as JsonObject, { ignoreUnknownFields: false });
  return validateSnapshot(value, sessionId);
}

export function parseSessionGoalOperation(value: unknown, sessionId: string, operationId: string): SessionGoalOperationResultDTO {
  fromJson(SessionGoalOperationResultDTOSchema, record(value) as JsonObject, { ignoreUnknownFields: false });
  validateSnapshot(value, sessionId);
  const result = value as SessionGoalOperationResultDTO;
  if (result.operation_id !== operationId || !["applied", "already_missing", "unknown"].includes(result.status)) {
    throw new Error("Goal 回执操作身份或状态无效");
  }
  return result;
}

export type GoalStreamMutation =
  | { kind: "updated"; goal: SessionGoal }
  | { kind: "cleared"; controlRevision: number; syncVersion: number }
  | null;

export function goalStreamMutation(
  event: { type: string; session_id?: string; thread_id?: string; payload?: Record<string, unknown>; raw?: Record<string, unknown> },
  owner: { session_id: string; thread_id: string },
): GoalStreamMutation {
  if (event.type !== "goal_updated" && event.type !== "goal_cleared") {
    return null;
  }
  const rawPayload = event.raw?.payload;
  const payload = event.payload
    ?? (rawPayload && typeof rawPayload === "object"
      ? rawPayload as Record<string, unknown>
      : undefined);
  assertGoalOwner(event, owner);
  assertGoalOwner(payload ?? {}, owner);
  if (event.type === "goal_cleared") {
    const controlRevision = payload?.control_revision;
    const syncVersion = payload?.sync_version;
    if (!Number.isSafeInteger(controlRevision) || (controlRevision as number) < 0
      || !Number.isSafeInteger(syncVersion) || (syncVersion as number) < 0) {
      throw new Error("goal_cleared 事件缺少同步版本");
    }
    return { kind: "cleared", controlRevision: controlRevision as number, syncVersion: syncVersion as number };
  }
  const value = payload?.goal;
  if (!value || typeof value !== "object") {
    throw new Error("goal_updated 事件缺少 goal 对象");
  }
  const goal = parseSessionGoal(value);
  assertGoalOwner(goal, owner);
  return {
    kind: "updated",
    goal,
  };
}

/** Goal 镜像只能应用权威 Session main Thread 的完整响应或事件。 */
export function assertGoalOwner(
  value: { session_id?: unknown; thread_id?: unknown },
  owner: { session_id: string; thread_id: string },
): void {
  if (!owner.thread_id || value.session_id !== owner.session_id || value.thread_id !== owner.thread_id) {
    throw new Error("Goal 归属与 Session main Thread 不一致");
  }
}

import type {
  SessionGoal,
  SessionGoalStatus,
  SessionGoalUpdateRequest,
} from "../types/backend";

export type GoalSlashAction =
  | { kind: "show" }
  | { kind: "edit" }
  | { kind: "pause" }
  | { kind: "resume" }
  | { kind: "clear" }
  | { kind: "create"; objective: string };

export type GoalStreamMutation =
  | { kind: "updated"; goal: SessionGoal }
  | { kind: "cleared" }
  | null;

const GOAL_STATUSES = new Set<SessionGoalStatus>([
  "active",
  "paused",
  "blocked",
  "usage_limited",
  "budget_limited",
  "complete",
]);

function isGoalStatus(value: unknown): value is SessionGoalStatus {
  return typeof value === "string" && GOAL_STATUSES.has(value as SessionGoalStatus);
}

export function goalStreamMutation(
  event: { type: string; payload?: Record<string, unknown>; raw?: Record<string, unknown> },
): GoalStreamMutation {
  if (event.type === "goal_cleared") {
    return { kind: "cleared" };
  }
  if (event.type !== "goal_updated") {
    return null;
  }
  const rawPayload = event.raw?.payload;
  const payload = event.payload
    ?? (rawPayload && typeof rawPayload === "object"
      ? rawPayload as Record<string, unknown>
      : undefined);
  const value = payload?.goal;
  if (!value || typeof value !== "object") {
    throw new Error("goal_updated 事件缺少 goal 对象");
  }
  const goal = value as Record<string, unknown>;
  if (
    typeof goal.goal_id !== "string"
    || typeof goal.session_id !== "string"
    || typeof goal.objective !== "string"
    || !isGoalStatus(goal.status)
    || (goal.token_budget !== null && typeof goal.token_budget !== "number")
    || typeof goal.tokens_used !== "number"
    || typeof goal.time_used_seconds !== "number"
    || typeof goal.created_at !== "string"
    || typeof goal.updated_at !== "string"
  ) {
    throw new Error("goal_updated 事件中的 goal 字段无效");
  }
  return {
    kind: "updated",
    goal: {
      goal_id: goal.goal_id,
      session_id: goal.session_id,
      objective: goal.objective,
      status: goal.status,
      token_budget: goal.token_budget,
      tokens_used: goal.tokens_used,
      time_used_seconds: goal.time_used_seconds,
      created_at: goal.created_at,
      updated_at: goal.updated_at,
    },
  };
}

export const GOAL_STATUS_LABELS: Record<SessionGoalStatus, string> = {
  active: "进行中",
  paused: "已暂停",
  blocked: "已阻塞",
  usage_limited: "用量受限",
  budget_limited: "预算已用尽",
  complete: "已完成",
};

export function parseGoalSlashAction(args: string): GoalSlashAction {
  const objective = args.trim();
  if (!objective) {
    return { kind: "show" };
  }
  switch (objective.toLowerCase()) {
    case "edit":
      return { kind: "edit" };
    case "pause":
      return { kind: "pause" };
    case "resume":
      return { kind: "resume" };
    case "clear":
      return { kind: "clear" };
    default:
      return { kind: "create", objective };
  }
}

export function goalEditStatus(status: SessionGoalStatus): SessionGoalStatus {
  return status === "complete" || status === "budget_limited"
    ? "active"
    : status;
}

export function goalNeedsReplacementConfirmation(status: SessionGoalStatus): boolean {
  return status !== "complete";
}

export function goalCanResume(status: SessionGoalStatus): boolean {
  return status === "paused" || status === "blocked" || status === "usage_limited";
}

export function restartCompletedGoalPayload(
  goal: SessionGoal,
): SessionGoalUpdateRequest {
  if (goal.status !== "complete") {
    throw new Error("只有已完成的 Goal 可以重新开始");
  }
  return {
    objective: goal.objective,
    status: "active",
    token_budget: goal.token_budget,
    replace: true,
  };
}

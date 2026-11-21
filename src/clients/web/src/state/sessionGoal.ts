import { t } from "../../../shared/core/i18n/index.js";
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

export const GOAL_STATUS_LABELS: Record<SessionGoalStatus, string> = {
  active: t("goal.status.active"),
  paused: t("goal.status.paused"),
  blocked: t("goal.status.blocked"),
  usage_limited: t("goal.status.usageLimited"),
  budget_limited: t("goal.status.budgetLimited"),
  complete: t("goal.status.complete"),
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

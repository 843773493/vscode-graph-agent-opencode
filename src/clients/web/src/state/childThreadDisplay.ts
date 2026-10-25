import type { ChildThreadStatus, InternalExecutionHistory, TurnHistoryPage } from "../types/protocol";
import { compareInternalResponseParts } from "./timeline/responseParts";

const STATUS_LABELS: Record<ChildThreadStatus, string> = {
  idle: "等待新消息",
  pending: "等待启动",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
};

export function childThreadStatusLabel(status: ChildThreadStatus): string {
  return STATUS_LABELS[status];
}

/** 状态徽标的配色类名；与 styles/session/childThreadPanel.css 中的状态变量对应。 */
const STATUS_CLASSES: Record<ChildThreadStatus, string> = {
  idle: "child-thread-status-idle",
  pending: "child-thread-status-pending",
  running: "child-thread-status-running",
  completed: "child-thread-status-completed",
  failed: "child-thread-status-failed",
};

export function childThreadStatusClass(status: ChildThreadStatus): string {
  return STATUS_CLASSES[status];
}

function mergeDisplayParts(
  previous: InternalExecutionHistory["display_parts"],
  page: InternalExecutionHistory["display_parts"],
): InternalExecutionHistory["display_parts"] {
  const parts = new Map<string, InternalExecutionHistory["display_parts"][number]>();
  for (const part of previous) parts.set(part.item_id, part);
  for (const part of page) parts.set(part.item_id, part);
  return [...parts.values()].sort(
    (left, right) => left.logical_item_ordinal - right.logical_item_ordinal,
  );
}

function mergeResponseParts(
  previous: InternalExecutionHistory["response_parts"],
  page: InternalExecutionHistory["response_parts"],
): InternalExecutionHistory["response_parts"] {
  const parts = new Map<string, InternalExecutionHistory["response_parts"][number]>();
  for (const part of previous) parts.set(part.part_id, part);
  for (const part of page) parts.set(part.part_id, part);
  return [...parts.values()].sort(compareInternalResponseParts);
}

/** 同一冻结历史游标的续页只合并展示成员，每个权威对象使用最新完整投影。 */
export function mergeChildThreadHistory(previous: TurnHistoryPage | null, page: TurnHistoryPage): TurnHistoryPage {
  if (!previous || previous.projection_epoch !== page.projection_epoch) return page;
  const turns = new Map<string, TurnHistoryPage["items"][number]>();
  for (const turn of previous.items) turns.set(turn.turn_id, turn);
  for (const turn of page.items) turns.set(turn.turn_id, turn);
  const executions = new Map<string, InternalExecutionHistory>();
  for (const execution of previous.internal_executions ?? []) {
    executions.set(execution.execution_id, execution);
  }
  for (const execution of page.internal_executions ?? []) {
    const prior = executions.get(execution.execution_id);
    const merged: InternalExecutionHistory = prior ? {
      ...execution,
      display_parts: mergeDisplayParts(prior.display_parts, execution.display_parts),
      response_parts: mergeResponseParts(prior.response_parts, execution.response_parts),
    } : execution;
    executions.set(execution.execution_id, merged);
  }
  return {
    ...page,
    items: [...turns.values()].sort((left, right) => left.ordinal - right.ordinal),
    internal_executions: [...executions.values()].sort((left, right) => left.created_at.localeCompare(right.created_at)),
  };
}

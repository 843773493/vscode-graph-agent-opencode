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

/** 同一冻结历史游标的续页只合并展示成员，每个权威对象使用最新完整投影。 */
export function mergeChildThreadHistory(previous: TurnHistoryPage | null, page: TurnHistoryPage): TurnHistoryPage {
  if (!previous || previous.projection_epoch !== page.projection_epoch) return page;
  const turns = new Map(previous.items.map((turn) => [turn.turn_id, turn]));
  for (const turn of page.items) turns.set(turn.turn_id, turn);
  const executions = new Map((previous.internal_executions ?? []).map((execution) => [execution.execution_id, execution]));
  for (const execution of page.internal_executions ?? []) {
    const prior = executions.get(execution.execution_id);
    const mergeParts = <T extends { part_id: string }>(before: T[], after: T[]) =>
      [...new Map([...before, ...after].map((part) => [part.part_id, part])).values()];
    const merged: InternalExecutionHistory = prior ? {
      ...execution,
      display_parts: [...new Map([...prior.display_parts, ...execution.display_parts].map((part) => [part.item_id, part])).values()]
        .sort((left, right) => left.logical_item_ordinal - right.logical_item_ordinal),
      response_parts: mergeParts(prior.response_parts, execution.response_parts)
        .sort(compareInternalResponseParts),
    } : execution;
    executions.set(execution.execution_id, merged);
  }
  return {
    ...page,
    items: [...turns.values()].sort((left, right) => left.ordinal - right.ordinal),
    internal_executions: [...executions.values()].sort((left, right) => left.created_at.localeCompare(right.created_at)),
  };
}

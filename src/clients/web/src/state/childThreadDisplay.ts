import { t } from "../../../shared/core/i18n/index.js";
import type { ChildThreadStatus, TurnHistoryPage } from "../types/protocol";
import { mergeInternalExecutions, type InternalExecutionRecord } from "./session/internalExecutionHistory";

export type ChildThreadHistoryPage = Omit<TurnHistoryPage, "internal_executions"> & { internal_executions?: InternalExecutionRecord[] };

const STATUS_LABELS: Record<ChildThreadStatus, string> = {
  idle: t("child.idle"),
  pending: t("child.pending"),
  running: t("child.running"),
  completed: t("child.completed"),
  failed: t("child.failed"),
};

export function childThreadStatusLabel(status: ChildThreadStatus): string {
  return STATUS_LABELS[status];
}

/** 执行和回合状态仅在展示时翻译，未识别的新状态保留原值。 */
export function childExecutionStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    completed: t('child.status.completed'), completed_empty: t('child.status.completed_empty'),
    failed: t('child.status.failed'), interrupted: t('child.status.interrupted'),
    cancelled: t('child.status.cancelled'), execution_lost: t('child.status.execution_lost'),
    unknown: t('child.status.unknown'), pending: t('child.status.pending'), running: t('child.status.running'),
  };
  return labels[status] ?? status;
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
export function mergeChildThreadHistory(previous: ChildThreadHistoryPage | null, page: TurnHistoryPage): ChildThreadHistoryPage {
  if (!previous || previous.projection_epoch !== page.projection_epoch) return page;
  const turns = new Map<string, TurnHistoryPage["items"][number]>();
  for (const turn of previous.items) turns.set(turn.turn_id, turn);
  for (const turn of page.items) turns.set(turn.turn_id, turn);
  return {
    ...page,
    items: [...turns.values()].sort((left, right) => left.ordinal - right.ordinal),
    internal_executions: mergeInternalExecutions(previous.internal_executions ?? [], page.internal_executions ?? [])
      .sort((left, right) => left.created_at.localeCompare(right.created_at)),
  };
}

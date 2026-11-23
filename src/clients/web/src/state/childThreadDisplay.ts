import { t } from "../../../shared/core/i18n/index.js";
import type { ChildThreadStatus, TurnHistoryPage } from "../types/protocol";
import { mergeInternalExecutions, type InternalExecutionRecord } from "./session/internalExecutionHistory";
import { mergeCanonicalParts } from "./timeline/responseParts";

export type ChildThreadHistoryPage = Omit<TurnHistoryPage, "internal_executions" | "items"> & {
  internal_executions?: InternalExecutionRecord[];
  items: Array<TurnHistoryPage["items"][number] & {activityOutline?: {beforeCursor: string | null}}>;
};

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
export function mergeChildThreadHistory(previous: ChildThreadHistoryPage | null, page: TurnHistoryPage, fragment = false, outline = false): ChildThreadHistoryPage {
  if (fragment && (!previous || previous.source_view_id !== page.source_view_id || previous.source_view_revision !== page.source_view_revision
    || previous.thread_id !== page.thread_id || previous.projection_epoch !== page.projection_epoch)) throw new Error("子 Thread 详情来源已变化");
  if (!previous || previous.projection_epoch !== page.projection_epoch) return {
    ...page,
    internal_executions: mergeInternalExecutions([], page.internal_executions ?? [], page),
  };
  const turns = new Map<string, ChildThreadHistoryPage["items"][number]>();
  for (const turn of previous.items) turns.set(turn.turn_id, turn);
  for (const turn of page.items) {
    const current = turns.get(turn.turn_id);
    if (fragment && current) turns.set(turn.turn_id, {...current,
      response_parts: mergeCanonicalParts(current.response_parts ?? [], turn.response_parts ?? []),
      ...(outline ? {activityOutline: {beforeCursor: page.before_cursor ?? null}} : {})});
    else if (!fragment) turns.set(turn.turn_id, turn);
  }
  return {
    ...(fragment ? previous : page),
    items: [...turns.values()].sort((left, right) => left.ordinal - right.ordinal),
    internal_executions: mergeInternalExecutions(previous.internal_executions ?? [], page.internal_executions ?? [], page)
      .map(execution => outline && page.internal_executions?.some(item => item.execution_id === execution.execution_id)
        ? {...execution, activityOutline: {beforeCursor: page.before_cursor ?? null}} : execution)
      .sort((left, right) => left.created_at.localeCompare(right.created_at)),
  };
}

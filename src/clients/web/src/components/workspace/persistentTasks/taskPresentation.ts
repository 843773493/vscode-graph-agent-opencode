import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
import type { PersistentTaskDTO } from "../../../api/persistentTasks";
import { formatLocalTimestamp } from "../../../utils/format";

const labels: Record<string, string> = {
  active: t("tasks.state.active"), paused: t("tasks.state.paused"), complete: t("tasks.state.complete"), terminated: t("tasks.state.terminated"), needs_attention: t("tasks.state.needsAttention"),
  waiting_trigger: t("tasks.state.waitingTrigger"), recovering: t("tasks.state.recovering"), pending: t("tasks.state.pending"), checking: t("tasks.state.checking"), delivering: t("tasks.state.delivering"),
  delivered: t("tasks.state.delivered"), checked: t("tasks.state.checked"), failed: t("tasks.state.failed"), suppressed: t("tasks.state.suppressed"), skipped: t("tasks.state.skipped"),
  wait: t("tasks.state.wait"), proceed: t("tasks.state.proceed"), blocked: t("tasks.state.blocked"), budget_limited: t("tasks.state.budgetLimited"), usage_limited: t("tasks.state.usageLimited"),
  manual: t("tasks.trigger.manual"), interval: t("tasks.trigger.interval"), cron: "Cron", external: t("tasks.trigger.external"), execution_end: t("tasks.trigger.executionEnd"),
  completed: t("tasks.state.complete"), completed_empty: t("tasks.state.completedEmpty"), cancelled: t("tasks.state.cancelled"), interrupted: t("tasks.state.interrupted"),
  unknown: t("tasks.state.unknown"),
};

export const taskLabel = (value: string) => labels[value] ?? value;

export const taskName = (task: PersistentTaskDTO) => task.configuration?.name ?? task.goal?.objective ?? task.task_id;
export function taskType(task: PersistentTaskDTO): string {
  if (task.application_type === "goal") return "Goal";
  if (task.application_type !== "automation") return task.application_type;
  const condition = task.configuration?.condition?.type_id;
  return condition === "builtin.script" ? t("tasks.type.script") : condition === "builtin.model" ? t("tasks.type.model") : t("tasks.type.automation");
}
export function taskSchedule(task: PersistentTaskDTO): string {
  if (task.application_type === "goal") return t("tasks.schedule.goal");
  const trigger = task.configuration?.trigger;
  const schedule = trigger?.type_id === "builtin.interval" ? t("tasks.schedule.interval", formatNumber(trigger.interval_seconds!))
    : trigger?.type_id === "builtin.cron" ? `${trigger.expression} · ${trigger.timezone}`
    : trigger ? taskLabel(trigger.type_id.replace("builtin.", "")) : t("tasks.schedule.unknown");
  return task.next_run_at ? t("tasks.schedule.next", schedule, formatLocalTimestamp(task.next_run_at)) : schedule;
}

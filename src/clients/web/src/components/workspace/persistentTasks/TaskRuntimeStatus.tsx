import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
import type { TaskRuntimeStatusDTO } from "../../../api/persistentTasks";

export default function TaskRuntimeStatus({ runtime }: { runtime: TaskRuntimeStatusDTO | null }) {
  if (!runtime) return null;
  const remaining = formatNumber(runtime.recovering_tasks);
  if (runtime.state === "failed") return <p role="alert">{t("tasks.runtime.failed", runtime.error, remaining)}</p>;
  if (runtime.state === "running") return <p role="status">{runtime.recovering_tasks
    ? t("tasks.runtime.runningRecovering", remaining) : t("tasks.runtime.running")}</p>;
  const labels: Record<string, string> = { starting: t("tasks.runtime.starting"), stopping: t("tasks.runtime.stopping"), stopped: t("tasks.runtime.stopped") };
  return <p role="status">{t("tasks.runtime.status", labels[runtime.state] ?? t("tasks.runtime.unknown", runtime.state), remaining)}</p>;
}

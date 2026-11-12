import { t } from "../../../../shared/core/i18n/index.js";
import * as api from "../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { errorMessage } from "../../utils/errorMessage";

export const taskActionMessageKeys = {
  details: "tasks.action.details", history: "tasks.action.history", edit: "tasks.action.edit",
  pause: "tasks.action.pause", resume: "tasks.action.resume", check: "tasks.action.check", checkOnly: "tasks.action.checkOnly",
  terminate: "tasks.action.terminate", update: "tasks.action.update",
} as const;
export type PersistentTaskAction = keyof typeof taskActionMessageKeys;
export const taskActionLabels = Object.fromEntries(Object.entries(taskActionMessageKeys)
  .map(([action, key]) => [action, t(key)])) as Record<PersistentTaskAction, string>;
export type TaskMutation = Exclude<PersistentTaskAction, "details" | "history" | "edit">;

/** 句柄属于发起组件与任务快照，命令不能转投到后来获得焦点的任务。 */
export interface PersistentTaskActionSource {
  target: GatewayWorkspaceRequestTarget;
  task: api.PersistentTaskDTO;
  run(action: PersistentTaskAction, configuration?: api.PersistentTaskConfigurationDTO): Promise<boolean>;
}
export type DispatchPersistentTaskAction = (action: PersistentTaskAction, source: PersistentTaskActionSource,
  configuration?: api.PersistentTaskConfigurationDTO) => Promise<boolean>;

export function availableTaskActions(task: api.PersistentTaskDTO): PersistentTaskAction[] {
  const actions: PersistentTaskAction[] = ["details", "history"];
  if (task.application_type !== "automation" || !task.configuration || task.lifecycle === "terminated" || task.lifecycle === "complete") return actions;
  return [...actions, task.lifecycle === "active" ? "pause" : "resume", "edit",
    ...(task.lifecycle === "active" ? ["check" as const] : []), "checkOnly", "terminate"];
}

export function requestTaskMutation(port: number, target: GatewayWorkspaceRequestTarget, task: api.PersistentTaskDTO,
  action: TaskMutation, signal: AbortSignal, configuration?: api.PersistentTaskConfigurationDTO): Promise<api.PersistentTaskDTO | null> {
  const required = action === "update" ? "edit" : action;
  if (!availableTaskActions(task).includes(required)) throw new Error(t("tasks.action.unavailable", taskActionLabels[action], task.lifecycle));
  if (action === "update") {
    if (!configuration) throw new Error(t("tasks.action.missingDefinition"));
    return api.updatePersistentTask(port, target, task, configuration, signal);
  }
  if (action === "check" || action === "checkOnly") {
    return api.checkPersistentTask(port, target, task, action === "checkOnly", signal).then(() => null);
  }
  return api.controlPersistentTask(port, target, task, action, signal);
}

/** 原 act 的唯一操作结算：完整回执先发布，成功或失败均向原 owner 回读，不自动重试写入。 */
export async function settleTaskOperation(port: number, target: GatewayWorkspaceRequestTarget, signal: AbortSignal,
  operation: () => Promise<api.PersistentTaskDTO | null>, onReceipt: (task: api.PersistentTaskDTO) => void,
  sessionId?: string) {
  let succeeded = false, error: string | null = null, snapshot: api.PersistentTaskSnapshot | null = null;
  try {
    const task = await operation();
    if (!signal.aborted) {
      if (task) onReceipt(task);
      succeeded = true;
    }
  } catch (cause) { error = errorMessage(cause); }
  if (!signal.aborted) {
    try { snapshot = await api.listPersistentTasks(port, target, signal, sessionId); }
    catch (cause) { error = error ? t("tasks.action.combinedError", error, t("tasks.action.refreshFailed", errorMessage(cause))) : t("tasks.action.refreshFailed", errorMessage(cause)); }
  }
  return { succeeded, error, snapshot };
}

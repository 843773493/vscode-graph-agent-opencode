import * as api from "../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { errorMessage } from "../../utils/errorMessage";

export const taskActionLabels = {
  details: "查看任务详情", history: "查看运行历史", edit: "编辑任务",
  pause: "暂停任务", resume: "恢复任务", check: "立即检查", checkOnly: "检查试运行",
  terminate: "终止任务", update: "保存任务",
} as const;
export type PersistentTaskAction = keyof typeof taskActionLabels;
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
  if (!availableTaskActions(task).includes(required)) throw new Error(`任务当前状态不支持${taskActionLabels[action]}：${task.lifecycle}`);
  if (action === "update") {
    if (!configuration) throw new Error("保存任务缺少完整定义");
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
    catch (cause) { error = [error, `刷新失败：${errorMessage(cause)}`].filter(Boolean).join("；"); }
  }
  return { succeeded, error, snapshot };
}

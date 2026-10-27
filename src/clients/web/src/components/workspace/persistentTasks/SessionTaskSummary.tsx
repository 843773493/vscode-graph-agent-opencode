import { useState } from "react";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import { usePersistentTasks } from "../../../hooks/persistentTasks/usePersistentTasks";
import { errorMessage } from "../../../utils/errorMessage";
import { taskLabel } from "./taskPresentation";
import TaskRuntimeStatus from "./TaskRuntimeStatus";

export default function SessionTaskSummary({ apiPort, target, sessionId, onOpenTask }: {
  apiPort: number; target: GatewayWorkspaceRequestTarget; sessionId: string;
  onOpenTask: (taskId: string) => Promise<void>;
}) {
  const state = usePersistentTasks(apiPort, target, sessionId);
  const [error, setError] = useState<string | null>(null);
  const open = async (taskId: string) => {
    setError(null);
    try { await onOpenTask(taskId); }
    catch (cause) { setError(errorMessage(cause)); }
  };
  return <section aria-label="当前会话关联任务">
    <h3>关联持久任务</h3>
    <button onClick={() => void state.refresh()} disabled={state.busy}>刷新关联任务</button>
    {state.error || error ? <p role="alert">{state.error || error}</p> : null}
    {!state.loaded && !state.error ? <p role="status">正在读取关联任务…</p> : null}
    <TaskRuntimeStatus runtime={state.runtime} />
    {state.loaded && !state.error && state.items.length === 0 ? <p>当前会话没有关联任务。</p> : null}
    <ul>{state.items.map((task) => <li key={task.task_id}>
      <button onClick={() => void open(task.task_id)}>{task.configuration?.name ?? task.goal?.objective ?? task.task_id}</button>
      <p>{taskLabel(task.lifecycle)} · {taskLabel(task.phase)}{task.recovering ? " · 后台恢复中" : ""}</p>
      {task.reason ? <p>{task.reason}</p> : null}
    </li>)}</ul>
  </section>;
}

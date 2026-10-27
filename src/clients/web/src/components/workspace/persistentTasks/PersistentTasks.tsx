import { taskLabel as label } from "./taskPresentation";
import { useEffect, useState } from "react";
import type { PersistentTaskDTO, TaskThreadTargetDTO } from "../../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import { useGenerationActivity } from "../../../hooks/persistentTasks/useGenerationActivity";
import { usePersistentTasks } from "../../../hooks/persistentTasks/usePersistentTasks";
import { useWarmConfirm } from "../../shell/WarmConfirmProvider";
import { errorMessage } from "../../../utils/errorMessage";
import TaskEditor, { configurationProblem, type TaskTemplate } from "./TaskEditor";
import TaskRunHistory from "./TaskRunHistory";
import "../../../styles/workspace/persistentTasks.css";

const time = (value: string | null | undefined) => value ? new Date(value).toLocaleString() : "—";
export default function PersistentTasks({ apiPort, target, goalSessionId, onManageGoal, onStatusChange, onOpenSession, onOpenThread, requestedTask }: {
  requestedTask?: { taskId: string } | null;
  apiPort: number; target: GatewayWorkspaceRequestTarget; goalSessionId: string | null;
  onOpenThread: (target: TaskThreadTargetDTO) => Promise<void>;
  onManageGoal: (sessionId: string) => Promise<void>; onStatusChange: (message: string) => void; onOpenSession: (sessionId: string) => Promise<void>;
}) {
  const state = usePersistentTasks(apiPort, target);
  useEffect(() => { if (requestedTask) state.select(requestedTask.taskId); }, [requestedTask, state.select]);
  const activityError = useGenerationActivity(apiPort, target, state.items, state.loaded, { onStatusChange, onOpenSession });
  const confirm = useWarmConfirm();
  const [editor, setEditor] = useState<{ task: PersistentTaskDTO | null; key: string; template: TaskTemplate | null } | null>(null);
  const [navigationError, setNavigationError] = useState<string | null>(null);
  const task = state.selected;
  const editable = task?.application_type === "automation" && task.configuration;
  const problem = editable ? configurationProblem(editable) : null;
  const terminal = task?.lifecycle === "terminated" || task?.lifecycle === "complete";
  const terminate = async (value: PersistentTaskDTO) => {
    if (await confirm({ title: "终止持久任务？", message: "将停止后续检查与投递。已经接受的工作执行继续由执行入口管理。", confirmText: "终止任务", danger: true })) await state.control(value, "terminate");
  };
  const manageGoal = async (sessionId: string) => {
    setNavigationError(null);
    try { await onManageGoal(sessionId); setEditor(null); }
    catch (cause) { setNavigationError(errorMessage(cause)); }
  };

  return <section className="persistent-tasks" aria-label="工作区持久任务">
    <header className="persistent-task-heading"><h2>持久任务</h2><button type="button" disabled={state.busy} onClick={() => setEditor({ task: null, key: crypto.randomUUID(), template: null })}>新建持久任务</button><button type="button" disabled={state.busy} onClick={() => void state.refresh()}>刷新任务</button></header>
    {navigationError ? <div role="alert">{navigationError}</div> : null}
    {activityError ? <div role="alert">{activityError}</div> : null}
    {state.error ? <div role="alert">{state.error}</div> : null}
    {!state.loaded && !state.error ? <p role="status">正在读取工作区任务…</p> : null}
    {state.loaded ? <p role="status">{state.items.filter((item) => item.recovering).length ? `${state.items.filter((item) => item.recovering).length} 个任务仍在恢复；其他任务独立推进。` : `当前工作区共 ${state.items.length} 个任务。`}</p> : null}
    <div className="persistent-task-layout">
      <div className="persistent-task-list" aria-label="持久任务列表">
        {state.loaded && state.items.length === 0 ? <p>还没有持久任务。</p> : null}
        {state.items.map((item) => <button type="button" key={item.task_id} aria-pressed={task?.task_id === item.task_id} onClick={() => { state.select(item.task_id); setEditor(null); }}>
          <strong>{item.configuration?.name ?? item.goal?.objective ?? item.task_id}</strong>
          <span>{item.application_type === "goal" ? "Goal" : "自动化"} · {label(item.lifecycle)} · {label(item.phase)}</span>
          {item.reason ? <span>{item.reason}</span> : null}
          <span>下次触发：{time(item.next_run_at)}</span>
          <small>{item.configuration?.action?.target_uri ?? item.configuration?.action?.generation?.session_strategy?.mode ?? item.goal?.session_id}</small>
        </button>)}
      </div>
      <div className="persistent-task-detail" aria-label="持久任务详情">
        {editor?.template === null ? <section aria-label="选择持久任务模板">
          <h3>选择持久任务模板</h3>
          <div className="persistent-task-actions">
            <button disabled={!goalSessionId} onClick={() => { if (goalSessionId) void manageGoal(goalSessionId); }}>Goal</button>
            {([['scheduled', '定时执行'], ['script', '脚本条件'], ['model', '模型判断'], ['custom', '自定义']] as const).map(([template, title]) => <button key={template} onClick={() => setEditor({ ...editor, template })}>{title}</button>)}
            <button onClick={() => setEditor(null)}>取消新建</button>
          </div>
          <p>Goal 使用当前会话主 Thread 的目标与预算；请先在当前工作区选择会话。其他模板可指定工作 Thread，保存后继续使用同一完整任务定义。</p>
        </section> : editor ? <TaskEditor apiPort={apiPort} target={target} key={editor.key} template={editor.template} configuration={editor.task?.configuration ?? undefined} busy={state.busy}
          onCancel={() => setEditor(null)} onSave={(configuration) => editor.task ? state.update(editor.task, configuration) : state.create(configuration, editor.key)} /> : task ? <>
          <h3>{task.configuration?.name ?? task.goal?.objective}</h3>
          <p>{label(task.lifecycle)} · {label(task.phase)}{task.recovering ? " · 正在按持久进度恢复，工作区就绪不代表此任务已恢复完成。" : ""}</p>
          {task.reason ? <p>{task.reason}</p> : null}
          {editable ? <>
            <div className="persistent-task-actions">
              {task.lifecycle === "active" ? <button disabled={state.busy} onClick={() => void state.control(task, "pause")}>暂停任务</button> : !terminal ? <button disabled={state.busy} onClick={() => void state.control(task, "resume")}>恢复任务</button> : null}
              <button disabled={state.busy || terminal || Boolean(problem)} onClick={() => setEditor({ task, key: crypto.randomUUID(), template: "scheduled" })}>编辑任务</button>
              <button disabled={state.busy || task.lifecycle !== "active"} onClick={() => void state.check(task, false)}>立即检查</button>
              <button disabled={state.busy || terminal} onClick={() => void state.check(task, true)}>检查试运行</button>
              <button disabled={state.busy || terminal} onClick={() => void terminate(task)}>终止任务</button>
            </div>
            {problem ? <div role="alert">{problem}</div> : null}
            <p>试运行会实际执行所属工作区的脚本或判断 Thread 的模型，仅检查条件，不执行后续工作动作。</p>
            <dl><dt>工作目标</dt><dd><code>{editable.action?.target_uri ?? editable.action?.generation?.session_strategy?.mode}</code></dd><dt>下次触发</dt><dd>{time(task.next_run_at)}</dd><dt>提示内容（当前定义）</dt><dd><pre>{editable.context?.content}</pre></dd></dl>
            <details><summary>完整任务定义</summary><pre>{JSON.stringify(editable, null, 2)}</pre></details>
            <h4>运行记录</h4>
            {state.history?.loading ? <p role="status">正在读取运行记录…</p> : state.history?.error ? <div role="alert">{state.history.error}</div> : state.history?.runs.length ? <TaskRunHistory apiPort={apiPort} target={target} runs={state.history.runs} onOpenSession={onOpenSession} onOpenThread={onOpenThread} /> : <p>暂无运行记录。</p>}
          </> : task.goal ? <>
            <p>目标属于会话 <code>{task.goal.session_id}</code> 的主 Thread。目标与预算在该会话右侧的 Goal 标签管理。</p>
            <button onClick={() => void manageGoal(task.goal!.session_id)}>管理会话 Goal</button>
            <p>已用 token：{task.goal.tokens_used} · 已用时间：{task.goal.time_used_seconds} 秒</p>
          </> : <div role="alert">不支持的任务应用：{task.application_type} / {task.application_version}</div>}
        </> : <p>选择任务查看详情，或创建新的持久任务。</p>}
      </div>
    </div>
  </section>;
}

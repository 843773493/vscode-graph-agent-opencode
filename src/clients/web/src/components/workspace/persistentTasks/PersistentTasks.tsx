import { useState } from "react";
import type { PersistentTaskDTO, TaskThreadTargetDTO } from "../../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import { getSessionMainThread } from "../../../api/session/sessions";
import { getWorkspace } from "../../../api/workspace";
import { useGenerationActivity } from "../../../hooks/persistentTasks/useGenerationActivity";
import { usePersistentTasks } from "../../../hooks/persistentTasks/usePersistentTasks";
import { useWarmConfirm } from "../../shell/WarmConfirmProvider";
import { errorMessage } from "../../../utils/errorMessage";
import TaskEditor, { configurationProblem, type TaskTemplate } from "./TaskEditor";
import TaskGoalDetail from "./TaskGoalDetail";
import TaskRunHistory from "./TaskRunHistory";
import TaskRuntimeStatus from "./TaskRuntimeStatus";
import { taskLabel as label, taskName, taskSchedule, taskType } from "./taskPresentation";

const time = (value: string | null | undefined) => value ? new Date(value).toLocaleString() : "—";
type Editor = { task: PersistentTaskDTO | null; key: string; template: TaskTemplate | "goal" | null; targetUri?: string; saveFailed?: boolean };
export default function PersistentTasks({ apiPort, target, sessionId, goalSessionId, onStatusChange, onOpenSession, onOpenThread, active = true }: {
  apiPort: number; target: GatewayWorkspaceRequestTarget; sessionId?: string; goalSessionId: string | null;
  onOpenThread: (target: TaskThreadTargetDTO) => Promise<void>;
  onStatusChange: (message: string) => void; onOpenSession: (sessionId: string) => Promise<void>;
  active?: boolean;
}) {
  const state = usePersistentTasks(apiPort, target, sessionId, active);
  // 工作区入口负责生成动作的通知策略；会话视图只观察，避免双重通知或导航。
  const activityError = useGenerationActivity(apiPort, target, state.items, active && state.loaded && !sessionId, { onStatusChange, onOpenSession });
  const confirm = useWarmConfirm();
  const [editor, setEditor] = useState<Editor | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [navigationError, setNavigationError] = useState<string | null>(null);
  const task = state.selected;
  const editable = task?.application_type === "automation" && task.configuration;
  const problem = editable ? configurationProblem(editable) : null;
  const terminal = task?.lifecycle === "terminated" || task?.lifecycle === "complete";
  const detailOpen = Boolean(editor || task);
  const newGoalSessionId = sessionId ?? goalSessionId;
  const beginTemplate = async (template: TaskTemplate) => {
    setNavigationError(null); setPreparing(true);
    try {
      let targetUri: string | undefined;
      if (sessionId) {
        const [workspace, main] = await Promise.all([getWorkspace(apiPort, target.workspace_id), getSessionMainThread(apiPort, sessionId, target)]);
        targetUri = `boxteam://workspace/${encodeURIComponent(workspace.workspace_id)}/resources/session/${encodeURIComponent(main.session_id)}/threads/${encodeURIComponent(main.thread_id)}`;
      }
      setEditor({ task: null, key: crypto.randomUUID(), template, targetUri });
    } catch (cause) { setNavigationError(errorMessage(cause)); }
    finally { setPreparing(false); }
  };
  const terminate = async (value: PersistentTaskDTO) => {
    if (await confirm({ title: "终止持久任务？", message: "将停止后续检查与投递。已经接受的工作执行继续由执行入口管理。", confirmText: "终止任务", danger: true })) await state.control(value, "terminate");
  };
  const back = () => { setEditor(null); state.select(""); };

  return <section className={`persistent-tasks${sessionId ? " persistent-tasks-session" : ""}${detailOpen ? " has-detail" : ""}`} aria-label={sessionId ? "当前会话关联任务" : "工作区持久任务"}>
    <header className="persistent-task-heading">
      <div><h2>{sessionId ? "会话任务" : "持久任务"}</h2><span className="persistent-task-hint">{sessionId ? "仅显示与此会话关联的任务" : "当前工作区的全部任务"}</span></div>
      <button type="button" disabled={state.busy || preparing} onClick={() => { state.select(""); setEditor({ task: null, key: crypto.randomUUID(), template: null }); }}>新建持久任务</button>
      <button type="button" disabled={state.busy} onClick={() => void state.refresh()}>刷新任务</button>
    </header>
    {navigationError ? <div role="alert">{navigationError}</div> : null}
    {activityError ? <div role="alert">{activityError}</div> : null}
    {state.error ? <div role="alert">{state.error}</div> : null}
    {!state.loaded && !state.error ? <p role="status">正在读取{sessionId ? "会话" : "工作区"}任务…</p> : null}
    <TaskRuntimeStatus runtime={state.runtime} />
    {state.loaded ? <div className="persistent-task-overview"><span>{state.items.length} 个任务</span><span>{state.items.filter((item) => item.lifecycle === "active").length} 个已启用</span><span>{state.items.filter((item) => item.recovering).length} 个恢复中</span></div> : null}
    <div className="persistent-task-layout">
      <div className="persistent-task-list" aria-label="持久任务列表">
        {state.loaded && !state.error && state.items.length === 0 ? <div className="persistent-task-empty"><span className="codicon codicon-checklist" aria-hidden="true" /><strong>{sessionId ? "当前会话没有关联任务。" : "还没有持久任务。"}</strong><p>设置一个目标，或让任务在定时、脚本条件满足时继续工作。</p></div> : null}
        {state.items.map((item) => <button type="button" key={item.task_id} aria-label={taskName(item)} aria-pressed={task?.task_id === item.task_id} onClick={() => { state.select(item.task_id); setEditor(null); }}>
          <span className="persistent-task-row-heading"><strong>{taskName(item)}</strong><span className={`persistent-task-badge is-${item.lifecycle}`}>{label(item.lifecycle)}</span></span>
          <span>{taskType(item)} · {item.recovering ? "正在恢复" : label(item.phase)}</span>
          {item.reason ? <span className="persistent-task-reason">{item.reason}</span> : null}
          <span className="persistent-task-hint">{taskSchedule(item)}</span>
        </button>)}
      </div>
      <div className="persistent-task-detail" aria-label="持久任务详情">
        {detailOpen ? <button className="persistent-task-back" type="button" disabled={preparing} onClick={back}>返回任务列表</button> : null}
        {editor?.template === null ? <section aria-label="选择持久任务模板">
          <h3>希望任务如何继续？</h3>
          <div className="persistent-task-templates">
            <button aria-label="Goal" disabled={!newGoalSessionId || preparing} onClick={() => setEditor({ ...editor, template: "goal" })}><strong>Goal</strong><span>围绕目标持续执行，保留预算和用量</span></button>
            {([['scheduled', '定时执行', '按间隔或计划执行工作'], ['script', '脚本条件', '例如服务上线后，携带提示继续'], ['model', '模型判断', '让独立 Thread 判断是否继续'], ['custom', '自定义', '组合触发、条件与执行动作']] as const).map(([template, title, hint]) => <button aria-label={title} disabled={preparing} key={template} onClick={() => void beginTemplate(template)}><strong>{title}</strong><span>{hint}</span></button>)}
          </div>
          {!newGoalSessionId ? <p className="persistent-task-hint">创建 Goal 前，请先选择当前工作区的会话。</p> : null}
          {preparing ? <p role="status">正在取得当前会话的工作目标…</p> : null}
          <button disabled={preparing} onClick={() => setEditor(null)}>取消新建</button>
        </section> : editor?.template === "goal" && newGoalSessionId ? <TaskGoalDetail key={`${target.workspace_id}:${newGoalSessionId}`} apiPort={apiPort} workspaceId={target.workspace_id} sessionId={newGoalSessionId} revision={state.items.find((item) => item.goal?.session_id === newGoalSessionId)?.revision} />
        : editor && editor.template && editor.template !== "goal" ? <TaskEditor apiPort={apiPort} target={target} key={editor.key} template={editor.template} initialTargetUri={editor.targetUri} configuration={editor.task?.configuration ?? undefined} busy={state.busy}
          onCancel={() => setEditor(null)} onSave={async (configuration) => {
            // 首次提交冻结编辑开始时的版本；失败重读后，用户重试才采用最新版本，草稿不重建。
            const base = editor.task && editor.saveFailed
              ? state.items.find(item => item.task_id === editor.task!.task_id) ?? editor.task : editor.task;
            const saved = base ? await state.update(base, configuration) : await state.create(configuration, editor.key);
            if (!saved) setEditor(current => current?.key === editor.key ? { ...current, saveFailed: true } : current);
            return saved;
          }} /> : task ? <>
          <h3>{taskName(task)}</h3>
          <p><span className={`persistent-task-badge is-${task.lifecycle}`}>{label(task.lifecycle)}</span> {label(task.phase)}</p>
          {task.recovering ? <p role="status">正在按持久进度恢复，请等待原执行对账完成；其他任务可独立推进。</p> : null}
          {task.reason ? <p className="persistent-task-reason">{task.reason}</p> : null}
          {editable ? <>
            <div className="persistent-task-actions">
              {task.lifecycle === "active" ? <button disabled={state.busy} onClick={() => void state.control(task, "pause")}>暂停任务</button> : !terminal ? <button disabled={state.busy} onClick={() => void state.control(task, "resume")}>恢复任务</button> : null}
              <button disabled={state.busy || terminal || Boolean(problem)} onClick={() => setEditor({ task, key: crypto.randomUUID(), template: "scheduled" })}>编辑任务</button>
              <button disabled={state.busy || task.lifecycle !== "active"} onClick={() => void state.check(task, false)}>立即检查</button>
              <button disabled={state.busy || terminal} onClick={() => void state.check(task, true)}>检查试运行</button>
              <button disabled={state.busy || terminal} onClick={() => void terminate(task)}>终止任务</button>
            </div>
            {problem ? <div role="alert">{problem}</div> : null}
            <dl><dt>触发方式</dt><dd>{taskSchedule(task)}</dd><dt>工作目标</dt><dd><code>{editable.action?.target_uri ?? editable.action?.generation?.session_strategy?.mode}</code></dd><dt>下次触发</dt><dd>{time(task.next_run_at)}</dd><dt>提示内容（当前定义）</dt><dd><pre>{editable.context?.content || "（由生成动作构造）"}</pre></dd></dl>
            <details><summary>高级信息与完整定义</summary><p>试运行会实际执行所属工作区的脚本或判断 Thread 的模型，仅检查条件，不执行后续工作动作。</p><code>任务 ID：{task.task_id}</code><pre>{JSON.stringify(editable, null, 2)}</pre></details>
          </> : task.goal ? <>
            <TaskGoalDetail key={`${target.workspace_id}:${task.goal.session_id}`} apiPort={apiPort} workspaceId={target.workspace_id} sessionId={task.goal.session_id} revision={task.revision} />
            <p className="persistent-task-hint">工作目标：关联会话的主线程</p>
            <button type="button" onClick={() => {
              setNavigationError(null);
              void onOpenSession(task.goal!.session_id).catch((cause: unknown) => setNavigationError(errorMessage(cause)));
            }}>打开目标会话</button>
          </> : <div role="alert">不支持的任务应用：{task.application_type} / {task.application_version}</div>}
          <h4>运行记录</h4>
          {state.history?.loading ? <p role="status">正在读取运行记录…</p> : state.history?.error ? <div role="alert">{state.history.error}</div> : state.history?.runs.length ? <TaskRunHistory apiPort={apiPort} target={target} runs={state.history.runs} onOpenSession={onOpenSession} onOpenThread={onOpenThread} /> : <p className="persistent-task-hint">暂无运行记录。</p>}
        </> : <div className="persistent-task-empty"><span className="codicon codicon-checklist" aria-hidden="true" /><p>选择任务查看详情，或创建新的持久任务。</p></div>}
      </div>
    </div>
  </section>;
}

import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
import { gatewayWorkspaceKey } from "../../../api/workspaceIdentity";
import { formatLocalTimestamp } from "../../../utils/format";
import { useEffect, useRef, useState } from "react";
import type { PersistentTaskDTO, PersistentTaskConfigurationDTO, TaskThreadTargetDTO } from "../../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import { getSessionMainThread } from "../../../api/session/sessions";
import { getWorkspace } from "../../../api/workspace";
import { useGenerationActivity } from "../../../hooks/persistentTasks/useGenerationActivity";
import { usePersistentTasks } from "../../../hooks/persistentTasks/usePersistentTasks";
import type { DispatchPersistentTaskAction, PersistentTaskAction, PersistentTaskActionSource } from "../../../hooks/persistentTasks/taskActions";
import { errorMessage } from "../../../utils/errorMessage";
import TaskEditor, { configurationProblem, type TaskTemplate } from "./TaskEditor";
import TaskGoalDetail from "./TaskGoalDetail";
import TaskRunHistory from "./TaskRunHistory";
import TaskRuntimeStatus from "./TaskRuntimeStatus";
import { taskLabel as label, taskName, taskSchedule, taskType } from "./taskPresentation";

const time = (value: string | null | undefined) => value ? formatLocalTimestamp(value) : "—";
type Editor = { task: PersistentTaskDTO | null; key: string; template: TaskTemplate | "goal" | null; targetUri?: string; saveFailed?: boolean };
export default function PersistentTasks({ apiPort, target, sessionId, goalSessionId, onStatusChange, onOpenSession, onOpenThread, onAction, intent, active = true }: {
  apiPort: number; target: GatewayWorkspaceRequestTarget; sessionId?: string; goalSessionId: string | null;
  onOpenThread: (target: TaskThreadTargetDTO) => Promise<void>;
  onStatusChange: (message: string) => void; onOpenSession: (sessionId: string) => Promise<void>;
  active?: boolean;
  onAction: DispatchPersistentTaskAction;
  intent?: { task: PersistentTaskDTO; action: "details" | "history" | "edit"; sequence: number };
}) {
  // 工作区入口负责生成动作的通知策略；会话视图只观察，避免双重通知或导航。
  const activity = useGenerationActivity(apiPort, target, active && !sessionId && !intent, { onStatusChange, onOpenSession });
  const state = usePersistentTasks(apiPort, target, sessionId, active, activity.observe);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [navigationError, setNavigationError] = useState<string | null>(null);
  const historyHeading = useRef<HTMLHeadingElement>(null);
  const focusedHistory = useRef<number>();
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
        const [workspace, main] = await Promise.all([getWorkspace(apiPort, gatewayWorkspaceKey(target)), getSessionMainThread(apiPort, sessionId, target)]);
        targetUri = `boxteam://workspace/${encodeURIComponent(workspace.workspace_id)}/resources/session/${encodeURIComponent(main.session_id)}/threads/${encodeURIComponent(main.thread_id)}`;
      }
      setEditor({ task: null, key: crypto.randomUUID(), template, targetUri });
    } catch (cause) { setNavigationError(errorMessage(cause)); }
    finally { setPreparing(false); }
  };
  const source = (value: PersistentTaskDTO): PersistentTaskActionSource => ({
    target: { ...target }, task: structuredClone(value),
    run: async (action, configuration) => {
      if (!state.isCurrent()) return false;
      if (action === "details" || action === "history" || action === "edit") {
        state.select(value.task_id);
        setEditor(action === "edit" ? { task: value, key: crypto.randomUUID(), template: "scheduled" } : null);
        return true;
      }
      return state.run(value, action, configuration);
    },
  });
  const dispatch = async (action: PersistentTaskAction, value: PersistentTaskDTO, configuration?: PersistentTaskConfigurationDTO) => {
    setNavigationError(null);
    try { return await onAction(action, source(value), configuration); }
    catch (cause) { setNavigationError(errorMessage(cause)); return false; }
  };
  useEffect(() => {
    if (!intent || !state.loaded) return;
    if (!state.items.some(item => item.task_id === intent.task.task_id)) { setNavigationError(t("tasks.list.missing")); return; }
    // 打开意图仅应用展示态；编辑保存仍使用用户接受时的原 revision。
    void source(intent.task).run(intent.action);
  }, [intent?.sequence, intent?.task.task_id, state.loaded]);
  useEffect(() => {
    if (intent?.action !== "history" || task?.task_id !== intent.task.task_id || focusedHistory.current === intent.sequence) return;
    focusedHistory.current = intent.sequence;
    historyHeading.current?.focus();
  }, [intent?.sequence, task?.task_id]);
  const back = () => { setEditor(null); state.select(""); };

  return <section className={`persistent-tasks panel-feedback-host${sessionId ? " persistent-tasks-session" : ""}${detailOpen ? " has-detail" : ""}`} aria-label={sessionId ? t("tasks.list.sessionAria") : t("tasks.list.workspaceAria")}>
    <header className="persistent-task-heading" aria-busy={!state.loaded || preparing}>
      <div><h2>{sessionId ? t("tasks.list.sessionTitle") : t("tasks.list.title")}</h2><span className="persistent-task-hint">{sessionId ? t("tasks.list.sessionHint") : t("tasks.list.workspaceHint")}</span></div>
      <button type="button" disabled={state.busy || preparing} onClick={() => { state.select(""); setEditor({ task: null, key: crypto.randomUUID(), template: null }); }}>{t("tasks.list.create")}</button>
      <button type="button" disabled={state.busy} onClick={() => void state.refresh()}><span className={`codicon codicon-refresh${!state.loaded ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />{t("tasks.list.refresh")}</button>
    </header>
    {navigationError || activity.error || state.error || state.history?.error ? <div className="panel-feedback" role="alert">{[navigationError, activity.error, state.error, state.history?.error].filter(Boolean).join("\n")}</div> : null}
    <div className="persistent-task-overview"><TaskRuntimeStatus runtime={state.runtime} /><span>{t("tasks.list.count", formatNumber(state.items.length))}</span><span>{t("tasks.list.activeCount", formatNumber(state.items.filter((item) => item.lifecycle === "active").length))}</span><span>{t("tasks.list.recoveringCount", formatNumber(state.items.filter((item) => item.recovering).length))}</span></div>
    <div className="persistent-task-layout">
      <div className="persistent-task-list" aria-label={t("tasks.list.aria")}>
        {state.loaded && !state.error && state.items.length === 0 ? <div className="persistent-task-empty"><span className="codicon codicon-checklist" aria-hidden="true" /><strong>{sessionId ? t("tasks.list.sessionEmpty") : t("tasks.list.empty")}</strong><p>{t("tasks.list.emptyHint")}</p></div> : null}
        {state.items.map((item) => <button type="button" key={item.task_id} aria-label={taskName(item)} aria-pressed={task?.task_id === item.task_id} onClick={() => void dispatch("details", item)}>
          <span className="persistent-task-row-heading"><strong>{taskName(item)}</strong><span className={`persistent-task-badge is-${item.lifecycle}`}>{label(item.lifecycle)}</span></span>
          <span>{taskType(item)} · {item.recovering ? t("tasks.list.recovering") : label(item.phase)}</span>
          {item.reason ? <span className="persistent-task-reason">{item.reason}</span> : null}
          <span className="persistent-task-hint">{taskSchedule(item)}</span>
        </button>)}
      </div>
      <div className="persistent-task-detail" aria-busy={Boolean(state.history?.loading)} aria-label={t("tasks.detail.aria")}>
        {detailOpen ? <button className="persistent-task-back" type="button" disabled={preparing} onClick={back}>{t("tasks.detail.back")}</button> : null}
        {editor?.template === null ? <section aria-label={t("tasks.template.aria")} aria-busy={preparing}>
          <h3>{t("tasks.template.title")}</h3>
          <div className="persistent-task-templates">
            <button aria-label="Goal" disabled={!newGoalSessionId || preparing} onClick={() => setEditor({ ...editor, template: "goal" })}><strong>Goal</strong><span>{t("tasks.template.goalHint")}</span></button>
            {([['scheduled', t("tasks.template.scheduled"), t("tasks.template.scheduledHint")], ['script', t("tasks.type.script"), t("tasks.template.scriptHint")], ['model', t("tasks.type.model"), t("tasks.template.modelHint")], ['custom', t("tasks.template.custom"), t("tasks.template.customHint")]] as const).map(([template, title, hint]) => <button aria-label={title} disabled={preparing} key={template} onClick={() => void beginTemplate(template)}><strong>{title}</strong><span>{hint}</span></button>)}
          </div>
          {!newGoalSessionId ? <p className="persistent-task-hint">{t("tasks.template.selectSession")}</p> : null}

          <button disabled={preparing} onClick={() => setEditor(null)}>{t("tasks.template.cancel")}</button>
        </section> : editor?.template === "goal" && newGoalSessionId ? <TaskGoalDetail key={`${gatewayWorkspaceKey(target)}:${newGoalSessionId}`} apiPort={apiPort} workspaceId={gatewayWorkspaceKey(target)} sessionId={newGoalSessionId} snapshot={state.sessionGoal} goal={state.items.find((item) => item.goal?.session_id === newGoalSessionId)?.goal ?? undefined} />
        : editor && editor.template && editor.template !== "goal" ? <TaskEditor apiPort={apiPort} target={target} key={editor.key} template={editor.template} initialTargetUri={editor.targetUri} configuration={editor.task?.configuration ?? undefined} busy={state.busy}
          onCancel={() => setEditor(null)} onSave={async (configuration) => {
            // 首次提交冻结编辑开始时的版本；失败重读后，用户重试才采用最新版本，草稿不重建。
            const base = editor.task && editor.saveFailed
              ? state.items.find(item => item.task_id === editor.task!.task_id) ?? editor.task : editor.task;
            const saved = base ? await dispatch("update", base, configuration) : await state.create(configuration, editor.key);
            if (!saved) setEditor(current => current?.key === editor.key ? { ...current, saveFailed: true } : current);
            return saved;
          }} /> : task ? <>
          <h3>{taskName(task)}</h3>
          <p><span className={`persistent-task-badge is-${task.lifecycle}`}>{label(task.lifecycle)}</span> {label(task.phase)}</p>

          {task.reason ? <p className="persistent-task-reason">{task.reason}</p> : null}
          {editable ? <>
            <div className="persistent-task-actions">
              {task.lifecycle === "active" ? <button disabled={state.busy} onClick={() => void dispatch("pause", task)}>{t("tasks.action.pause")}</button> : !terminal ? <button disabled={state.busy} onClick={() => void dispatch("resume", task)}>{t("tasks.action.resume")}</button> : null}
              <button disabled={state.busy || terminal || Boolean(problem)} onClick={() => void dispatch("edit", task)}>{t("tasks.action.edit")}</button>
              <button disabled={state.busy || task.lifecycle !== "active"} onClick={() => void dispatch("check", task)}>{t("tasks.action.check")}</button>
              <button disabled={state.busy || terminal} onClick={() => void dispatch("checkOnly", task)}>{t("tasks.action.checkOnly")}</button>
              <button disabled={state.busy || terminal} onClick={() => void dispatch("terminate", task)}>{t("tasks.action.terminate")}</button>
            </div>
            {problem ? <div role="alert">{problem}</div> : null}
            <dl><dt>{t("tasks.editor.trigger")}</dt><dd>{taskSchedule(task)}</dd><dt>{t("tasks.detail.workTarget")}</dt><dd><code>{editable.action?.target_uri ?? editable.action?.generation?.session_strategy?.mode}</code></dd><dt>{t("tasks.detail.nextRun")}</dt><dd>{time(task.next_run_at)}</dd><dt>{t("tasks.detail.content")}</dt><dd><pre>{editable.context?.content || t("tasks.detail.generatedContent")}</pre></dd></dl>
            <details><summary>{t("tasks.detail.advanced")}</summary><p>{t("tasks.detail.dryRunHint")}</p><code>{t("tasks.detail.taskId", task.task_id)}</code><pre>{JSON.stringify(editable, null, 2)}</pre></details>
          </> : task.goal ? <>
            <TaskGoalDetail key={`${gatewayWorkspaceKey(target)}:${task.goal.session_id}`} apiPort={apiPort} workspaceId={gatewayWorkspaceKey(target)} sessionId={task.goal.session_id} goal={task.goal} />
            <p className="persistent-task-hint">{t("tasks.detail.goalTarget")}</p>
            <button type="button" onClick={() => {
              setNavigationError(null);
              void onOpenSession(task.goal!.session_id).catch((cause: unknown) => setNavigationError(errorMessage(cause)));
            }}>{t("tasks.detail.openSession")}</button>
          </> : <div role="alert">{t("tasks.detail.unsupported", task.application_type, task.application_version)}</div>}
          <h4 ref={historyHeading} tabIndex={-1}>{t("tasks.history.title")}</h4>
          {state.history?.nextBefore ? <button disabled={state.history.loading} onClick={state.loadOlder}>{t("tasks.history.older")}</button> : null}
          {state.history?.runs.length ? <TaskRunHistory apiPort={apiPort} target={target} runs={state.history.runs} onOpenSession={onOpenSession} onOpenThread={onOpenThread} /> : !state.history?.loading && !state.history?.error ? <p className="persistent-task-hint">{t("tasks.history.empty")}</p> : null}

        </> : <div className="persistent-task-empty"><span className="codicon codicon-checklist" aria-hidden="true" /><p>{t("tasks.detail.empty")}</p></div>}
      </div>
    </div>
  </section>;
}

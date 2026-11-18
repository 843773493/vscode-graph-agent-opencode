import { useContext, useRef, useState } from "react";
import { t } from "../../../../shared/core/i18n/index.js";
import type { PersistentTaskDTO } from "../../api/persistentTasks";
import { availableTaskActions, taskActionLabels, type PersistentTaskAction } from "../../hooks/persistentTasks/taskActions";
import { taskLabel, taskName, taskSchedule, taskType } from "../workspace/persistentTasks/taskPresentation";
import { errorMessage } from "../../utils/errorMessage";
import AnchoredOverlay from "../overlays/AnchoredOverlay";
import { ComposerDiagnosticsContext } from "./ComposerDiagnosticsContext";

export interface ComposerTasks {
  items: readonly PersistentTaskDTO[];
  loaded: boolean;
  error: string | null;
  onAction: (task: PersistentTaskDTO, action: PersistentTaskAction) => Promise<unknown>;
  onManage: () => void;
}

/** 复用会话已有订阅与任务命令；这里只管理展开、菜单和操作反馈。 */
export default function ComposerTaskList({ items, loaded, error, onAction }: Omit<ComposerTasks, "onManage">) {
  const report = useContext(ComposerDiagnosticsContext);
  const [busy, setBusy] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [menu, setMenu] = useState<PersistentTaskDTO | null>(null);
  const anchor = useRef<HTMLButtonElement | null>(null);
  const run = async (task: PersistentTaskDTO, action: PersistentTaskAction) => {
    if (busy) return;
    setBusy(task.task_id); setFailed(null);
    try { await onAction(task, action); }
    catch (cause) { setFailed(task.task_id); report(`task_id=${task.task_id}: ${errorMessage(cause)}`); }
    finally { setBusy(null); }
  };
  return <div className="composer-task-list">
    {!loaded ? <p role="status">{t(error ? "composer.tasks.unloaded" : "tasks.list.loadingSession")}</p>
      : !items.length ? <p>{t("tasks.list.sessionEmpty")}</p> : null}
    <div role="list">
      {items.map(task => {
        const actions = availableTaskActions(task);
        const toggle = actions.includes("pause") ? "pause" : actions.includes("resume") ? "resume" : null;
        const detail = `${taskType(task)} · ${taskLabel(task.lifecycle)} · ${taskLabel(task.phase)}\n${taskSchedule(task)}${task.reason ? `\n${task.reason}` : ""}`;
        return <div key={task.task_id} className="composer-task-row" role="listitem" data-task-id={task.task_id}>
          <span className={`codicon ${task.application_type === "goal" ? "codicon-target" : "codicon-checklist"}`} aria-hidden="true" />
          <span className="composer-task-name" title={`${taskName(task)}\n${detail}`}>{taskName(task)}</span>
          <span className={`composer-task-status${failed === task.task_id ? " needs-attention" : ""}`}
            role={failed === task.task_id ? "alert" : undefined} title={failed === task.task_id ? t("composer.operationFailed") : detail}>
            {failed === task.task_id ? t("composer.operationFailed")
              : taskLabel(task.recovering ? "recovering" : task.lifecycle === "active" ? task.phase : task.lifecycle)}
          </span>
          <div className="composer-task-actions">
            {toggle ? <button type="button" className="composer-status-icon" disabled={Boolean(busy)}
              title={taskActionLabels[toggle]} aria-label={taskActionLabels[toggle]} aria-busy={busy === task.task_id}
              onClick={() => void run(task, toggle)}><span className={`codicon ${toggle === "pause" ? "codicon-debug-pause" : "codicon-play"}`} aria-hidden="true" /></button> : null}
            <button type="button" className="composer-status-icon" disabled={Boolean(busy)}
              title={taskActionLabels.details} aria-label={taskActionLabels.details} onClick={() => void run(task, "details")}>
              <span className="codicon codicon-open-preview" aria-hidden="true" /></button>
            <button type="button" className="composer-status-icon" disabled={Boolean(busy)} aria-haspopup="menu"
              aria-expanded={menu?.task_id === task.task_id} title={t("composer.status.more")} aria-label={t("composer.status.more")}
              onClick={event => { anchor.current = event.currentTarget; setMenu(current => current?.task_id === task.task_id ? null : task); }}>
              <span className="codicon codicon-ellipsis" aria-hidden="true" /></button>
          </div>
        </div>;
      })}
    </div>
    <AnchoredOverlay open={Boolean(menu)} anchorRef={anchor} placement="top-end" manageFocus onClose={() => setMenu(null)}>
      {menu ? <div className="composer-view-menu" role="menu" aria-label={t("composer.status.more")} onKeyDown={event => {
        if (event.key === "Escape") {
          event.preventDefault(); event.stopPropagation(); setMenu(null); return;
        }
        if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const controls = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("button"));
        const index = controls.findIndex(button => button === event.target);
        controls[event.key === "Home" ? 0 : event.key === "End" ? controls.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + controls.length) % controls.length]?.focus();
      }}>
        {availableTaskActions(menu).filter(action => action !== "details" && action !== "pause" && action !== "resume").map(action =>
          <button type="button" key={action} role="menuitem" className="composer-view-menu-item"
            onClick={() => { setMenu(null); void run(menu, action); }}>{taskActionLabels[action]}</button>)}
      </div> : null}
    </AnchoredOverlay>
  </div>;
}

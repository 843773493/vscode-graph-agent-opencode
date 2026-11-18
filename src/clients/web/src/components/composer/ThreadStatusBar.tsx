import React, { useContext, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { t } from "../../../../shared/core/i18n/index.js";
import { queuedThreadMessages, threadStatusSyncError, type ThreadStatusInput } from "../../state/threadStatus";
import QueueDetails from "../chat/pending/PendingQueueBar";
import { ComposerDiagnosticsContext } from "./ComposerDiagnosticsContext";
import ComposerQuestions from "./ComposerQuestions";
import { useUserQuestions } from "../../hooks/runtime/useUserQuestions";
import type { UserQuestionOwner } from "../../api/userQuestions";
import ComposerTaskList, { type ComposerTasks } from "./ComposerTaskList";

export interface ThreadStatusBarProps extends ThreadStatusInput {
  queueActions?: Pick<React.ComponentProps<typeof QueueDetails>, "onClear" | "onRemove" | "onChangePolicy" | "onReorder">;
  submission?: { count: number; attention: boolean; content: React.ReactNode };
  tasks?: ComposerTasks;
  questionOwner?: UserQuestionOwner;
}

type Section = "tasks" | "queue" | "questions";

/** 只组合既有 owner 的展示事实与操作，不注册轮询或保存业务状态。 */
export default function ThreadStatusBar({ queueActions, submission, tasks, questionOwner, ...input }: ThreadStatusBarProps) {
  const queued = queuedThreadMessages(input.conversations);
  const report = useContext(ComposerDiagnosticsContext);
  const [presentation, setPresentation] = useState<{ ids: Section[]; open: Section[] }>({ ids: [], open: [] });
  const questions = useUserQuestions(questionOwner, presentation.open.includes("questions"), report);
  const syncError = threadStatusSyncError(input);
  useEffect(() => { if (syncError) report(`${t("thread.status.syncFailed")}: ${syncError}`); }, [report, syncError]);
  useEffect(() => { if (tasks?.error) report(`${t("composer.tasks.title")}: ${tasks.error}`); }, [report, tasks?.error]);
  const panelPrefix = useId();
  const stack = useRef<HTMLDivElement>(null);
  const triggers = useRef<Partial<Record<Section, HTMLButtonElement | null>>>({});
  const items: { id: Section; title: string; label: string; icon: string; attention?: boolean; content: React.ReactNode }[] = [];
  if (tasks && (tasks.items.length > 0 || tasks.error)) items.push({ id: "tasks", title: t("composer.tasks.title"), icon: "codicon-checklist",
    label: tasks.loaded ? t("composer.tasks.count", tasks.items.length) : t("composer.tasks.title"),
    attention: Boolean(tasks.error), content: <ComposerTaskList {...tasks} /> });
  const queueCount = queued.length + (submission?.count ?? 0);
  if (queueCount > 0) items.push({ id: "queue", title: t("thread.status.queue"),
    label: t("thread.status.queueCount", queueCount), icon: "codicon-list-ordered", attention: submission?.attention,
    content: <>{queued.length > 0 ? queueActions
      ? <QueueDetails conversations={queued} {...queueActions} /> : <p>{t("thread.status.queueCount", queued.length)}</p> : null}
      {submission?.content}</> });

  if (questions.count > 0) items.push({ id: "questions", title: t("composer.questions.title"),
    label: t("composer.questions.count", questions.count), icon: "codicon-comment-discussion",
    content: presentation.open.includes("questions") ? <ComposerQuestions {...questions} /> : null });
  const ids = items.map(item => item.id);
  // 只在条目增删时协调展示态；普通状态更新保留手动折叠和首次出现顺序。
  if (ids.length !== presentation.ids.length || ids.some(id => !presentation.ids.includes(id))) {
    const added = ids.filter(id => !presentation.ids.includes(id));
    setPresentation({ ids: [...presentation.ids.filter(id => ids.includes(id)), ...added],
      open: [...presentation.open.filter(id => ids.includes(id)), ...added.filter(id => id === "queue")] });
  }
  const previousOpen = useRef(presentation.open);
  useLayoutEffect(() => {
    if (presentation.open.some(id => !previousOpen.current.includes(id)) && stack.current) stack.current.scrollTop = 0;
    previousOpen.current = presentation.open;
  }, [presentation.open]);
  const toggle = (id: Section) => setPresentation(current => ({ ...current,
    open: current.open.includes(id) ? current.open.filter(value => value !== id) : [...current.open, id] }));
  if (!items.length) return null;
  const orderedItems = presentation.ids.flatMap(id => items.filter(item => item.id === id));
  const panelOrder = [...presentation.open].reverse().concat(presentation.ids.filter(id => !presentation.open.includes(id)));
  return <div className="thread-status-region" data-testid="thread-status" aria-label={t("thread.status.label")}>
    <div className="thread-status">
      <div className="thread-status-stack" ref={stack}>
      {panelOrder.map(id => {
        const item = items.find(item => item.id === id);
        return item ? <section key={id} id={`${panelPrefix}-${id}`} className="thread-status-detail-body"
          role="region" aria-label={item.title} data-status-panel={id} hidden={!presentation.open.includes(id)}
          onKeyDown={event => {
            if (event.key !== "Escape" || event.defaultPrevented) return;
            event.preventDefault(); event.stopPropagation(); toggle(id); triggers.current[id]?.focus({ preventScroll: true });
          }}>{item.content}</section> : null;
      })}
    </div>
    <div className={`thread-status-row${presentation.open.length ? " has-details" : ""}`} role="group" aria-label={t("thread.status.label")}>
      {orderedItems.map(item => <button key={item.id} ref={node => { triggers.current[item.id] = node; }} type="button"
        data-status-section={item.id} className={`thread-status-trigger${item.attention ? " needs-attention" : ""}`}
        aria-label={item.label} title={item.label} aria-expanded={presentation.open.includes(item.id)} aria-controls={`${panelPrefix}-${item.id}`}
        onClick={() => toggle(item.id)}>
        <span className={`codicon ${item.icon}`} aria-hidden="true" /><span className="thread-status-label">{item.label}</span>
      </button>)}
      {tasks && ids.includes("tasks") ? <button type="button" className="composer-task-manage composer-status-icon"
        title={t("composer.tasks.manage")} aria-label={t("composer.tasks.manage")} onClick={tasks.onManage}>
        <span className="codicon codicon-settings-gear" aria-hidden="true" />
      </button> : null}
      </div>
      <span className="thread-status-announcement" role="status" aria-live="polite">{orderedItems.map(item => item.label).join(" · ")}</span>
    </div>
  </div>;
}

import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
import { useCallback, useRef, useState } from "react";
import type { ChildThreadList, ChildThreadSummary } from "../../../types/backend";
import { copyTextToClipboard } from "../../../utils/clipboard";
import { formatDateTime } from "../../../utils/format";
import {
  childThreadStatusClass,
  childThreadStatusLabel,
} from "../../../state/childThreadDisplay";
import { errorMessage } from "../../../utils/errorMessage";
import type { useChildThreadConversation } from "../../../hooks/sessionTurnHistory/useChildThreadConversation";
import ChildThreadConversation from "./ChildThreadConversation";
import type { ChildThreadForkPoint } from "./ChildThreadConversation";

/** child thread 在列表中的图标；用分支语义表达“从 owner 会话派生”。 */
const CHILD_THREAD_ICON = "codicon-git-branch";

/**
 * 主窗口左侧会话节点展开后的 Thread 导航。
 * Thread 属于当前 Session，不是导航子 Session 或运行连接；
 * 数据由 App 层加载后通过 props 传入，组件本身不持有业务状态权威。
 */
export default function ChildThreadPanel({
  threads,
  teams,
  total,
  loading,
  error,
  loadedAt,
  sessionId,
  ownerLabel,
  mainThreadId,
  activeThreadId,
  onRefresh,
  onSelectThread,
  onOpenMainThread,
  onOpenThread,
  onForkFromThread,
  conversation,
}: {
  threads: ChildThreadSummary[];
  teams: ChildThreadList["teams"];
  total: number;
  loading: boolean;
  error: string | null;
  loadedAt: string | null;
  sessionId: string;
  ownerLabel: string;
  /** 当前会话在权威 catalog 中解析出的 canonical main thread ID。 */
  mainThreadId: string | null;
  /** 当前调试 owner 的 thread id。 */
  activeThreadId: string | null;
  onRefresh: () => void;
  onSelectThread?: (threadId: string) => void;
  onOpenMainThread: () => void;
  onOpenThread: (thread: ChildThreadSummary) => void;
  onForkFromThread?: (threadId: string, point: ChildThreadForkPoint) => void;
  conversation: ReturnType<typeof useChildThreadConversation>;
}) {
  const [notice, setNotice] = useState("");
  const [noticeError, setNoticeError] = useState(false);
  const selectedThreadIdRef = useRef<string | null>(null);
  selectedThreadIdRef.current = conversation.selectedThread?.thread_id ?? null;
  const onForkFromThreadRef = useRef(onForkFromThread);
  onForkFromThreadRef.current = onForkFromThread;
  const forkSelectedThread = useCallback((point: ChildThreadForkPoint) => {
    const selectedThreadId = selectedThreadIdRef.current;
    if (selectedThreadId) onForkFromThreadRef.current?.(selectedThreadId, point);
  }, []);

  const handleCopyThreadId = (threadId: string) => {
    void copyTextToClipboard(threadId)
      .then(() => {
        setNoticeError(false);
        setNotice(t("child.copied", threadId));
      })
      .catch((copyError: unknown) => {
        setNoticeError(true);
        setNotice(
          t("child.copyFailed", errorMessage(copyError)),
        );
      });
  };

  return (
    <section className="panel-view child-thread-panel" aria-busy={loading} aria-label={t("child.panel")}>
      <div className="panel-header">
        <div
          className="panel-title"
          title={`${sessionId || t("child.noSession")}${loadedAt ? t("child.readAt", loadedAt) : ""}`}
        >

          {t("child.panel")} <span className="resource-total-count">{formatNumber(threads.length + (mainThreadId ? 1 : 0))}</span>
        </div>
        <div className="panel-header-meta">
          {total > threads.length ? <span>{t("child.total", formatNumber(total))}</span> : null}
        </div>
        <button
          type="button"
          className="resource-icon-button"
          onClick={onRefresh}
          disabled={loading || !sessionId}
          title={t("child.refresh")}
          aria-label={t("child.refresh")}
        >
          <span
            className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`}
            aria-hidden="true"
          />
        </button>
      </div>

      <div className="panel-header-meta" data-child-owner-session={sessionId}>
        {ownerLabel}
      </div>

      <div className="child-thread-notice-slot">
        {notice ? (
          <div
            className={`child-thread-notice${noticeError ? " is-error" : ""}`}
            role="status"
            title={notice}
          >
            <span>{notice}</span>
            <button
              type="button"
              className="child-thread-notice-close"
              aria-label={t("child.dismiss")}
              onClick={() => setNotice("")}
            >
              <span className="codicon codicon-close" aria-hidden="true" />
            </button>
          </div>
        ) : null}
      </div>


      {error ? (
        <div className="panel-feedback" role="alert">{t("child.loadFailed", error)}</div>
      ) : null}
      {!loading && !error && !mainThreadId && threads.length === 0 && teams.length === 0 && sessionId ? (
        <div className="empty-state">

          {t("child.empty")}
        </div>
      ) : null}
      {!loading && !error && threads.length === 0 && !sessionId ? (
        <div className="empty-state">{t("child.selectSession")}</div>
      ) : null}

      {(threads.length > 0 || teams.length > 0 || Boolean(sessionId)) ? (
        <div className="child-thread-list" role="list">
          {teams.map((team) => (
            <article key={team.team_id} className="child-thread-item" role="listitem" data-team-id={team.team_id} data-team-version={team.version}>
              <div className="child-thread-copy"><strong>{team.name}</strong><small>{t("child.teamVersion", formatNumber(team.version))}</small></div>
              <div className="child-thread-job-status">{t("child.coordinator", team.coordinator_thread_id)}</div>
              {team.members.map((member) => (
                <div key={member.thread_id} className="child-thread-runtime" data-team-member-thread-id={member.thread_id}>
                  {member.role} · {member.work_mode === "read_only" ? t("child.readOnly") : t("child.writable")} · {member.thread_id}
                </div>
              ))}
              {team.tasks.map((task) => (
                <div key={task.task_id} className="child-thread-runtime" data-team-task-id={task.task_id} data-team-task-status={task.status}>
                  <strong>{task.title}</strong> · {{ queued: t("child.queued"), in_progress: t("child.inProgress"), blocked: t("child.blocked"), completed: t("child.completed"), failed: t("child.failed"), cancelled: t("child.cancelled") }[task.status as "queued" | "in_progress" | "blocked" | "completed" | "failed" | "cancelled"]}
                  <div>{t("child.execution", task.assignee_thread_id, task.phase, formatNumber(task.cycle))}</div>
                  {task.summary ? <div>{task.summary}</div> : null}
                  {task.error ? <div role="alert">{task.error}</div> : null}
                </div>
              ))}
            </article>
          ))}
          {sessionId ? (
            <article
              className="child-thread-item child-thread-owner-item"
              role="listitem"
              data-thread-id={mainThreadId ?? undefined}
            >
              <div className="child-thread-row">
                <button
                  type="button"
                  className="child-thread-main"
                  onClick={() => {
                    if (mainThreadId) onOpenMainThread();
                  }}
                  disabled={!mainThreadId}
                  title={t("child.openMain")}
                  aria-label={t("child.openMain")}
                  aria-current={mainThreadId === activeThreadId ? "page" : undefined}
                >
                  <span
                    className="child-thread-icon codicon codicon-home"
                    aria-hidden="true"
                  />
                  <span className="child-thread-copy">
                    <strong title={t("child.mainDescription")}>{t("child.main")}</strong>
                  </span>
                  <span className="child-thread-status child-thread-status-running">
                    {mainThreadId !== null && mainThreadId === activeThreadId
                      ? t("child.currentThread")
                      : t("child.main")}
                  </span>
                </button>
              </div>
            </article>
          ) : null}
          {threads.map((thread) => {
            const isCurrent = thread.thread_id === activeThreadId;
            return (
              <article
                key={thread.thread_id}
                className="child-thread-item"
                role="listitem"
                data-start-status={thread.status}
                data-thread-id={thread.thread_id}
              >
                <div className="child-thread-row">
                  <button
                    type="button"
                    className="child-thread-main"
                    aria-current={isCurrent ? "page" : undefined}
                    onClick={() => onOpenThread(thread)}
                    title={t("child.openConversation", thread.title ?? thread.thread_id)}
                  >
                    <span
                      className={`child-thread-icon codicon ${CHILD_THREAD_ICON}`}
                      aria-hidden="true"
                    />
                    <span className="child-thread-copy">
                      <strong>{thread.title ?? thread.thread_id}</strong>
                      <small>
                        {thread.subagent_type ?? thread.role ?? "child thread"} · {formatDateTime(thread.created_at)}
                      </small>
                    </span>
                    <span
                      className={`child-thread-status ${childThreadStatusClass(thread.status)}`}
                    >
                      {childThreadStatusLabel(thread.status)}
                    </span>
                  </button>
                  {onSelectThread ? <button type="button" className="child-thread-copy-id" onClick={() => onSelectThread(thread.thread_id)} aria-label={t("child.debugAria", thread.thread_id)} title={t("child.debug")}>
                    <span className="codicon codicon-debug" aria-hidden="true" />
                  </button> : null}
                  {onForkFromThread ? <button type="button" className="child-thread-copy-id" onClick={() => onForkFromThread(thread.thread_id, {})} aria-label={t("child.fork")} title={t("child.fork")}>
                    <span className="codicon codicon-git-branch" aria-hidden="true" />
                  </button> : null}
                  <button
                    type="button"
                    className="child-thread-copy-id"
                    onClick={() => handleCopyThreadId(thread.thread_id)}
                    title={t("child.copyId", thread.thread_id)}
                    aria-label={t("child.copyId", thread.thread_id)}
                  >
                    <span className="codicon codicon-copy" aria-hidden="true" />
                  </button>
                </div>
                {thread.runtime ? (
                  <div className="child-thread-runtime">
                    {{ cold: t("child.runtime.cold"), loading: t("child.runtime.loading"), resident: t("child.runtime.resident"), unloading: t("child.runtime.unloading") }[thread.runtime.residency]}
                    {thread.runtime.cold_eligible ? t("child.runtime.eligible") : ""}
                    {thread.runtime.last_activity_at ? t("child.lastActivity", formatDateTime(thread.runtime.last_activity_at)) : ""}
                    {thread.runtime.idle_deadline ? t("child.idleDeadline", formatDateTime(thread.runtime.idle_deadline)) : ""}
                    {thread.runtime.blockers.map((blocker) => <div key={blocker.kind}>{blocker.reason}</div>)}
                  </div>
                ) : null}
                {thread.report_execution_id && thread.report_status && thread.report_status !== "unknown" ? (
                  <button type="button" className="child-thread-report-link" onClick={() => conversation.selectThread(thread.thread_id, "report")}>
                    {t(conversation.isUnread(thread) ? "child.newReport" : "child.report")}
                  </button>
                ) : null}
                {thread.admission_state || thread.collaboration_state ? (
                  <div className="child-thread-job-status">
                    {t("child.admission", thread.admission_state ?? t("child.unknown"), thread.collaboration_state ?? t("child.unknown"))}
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : null}
      {!loading && !error && loadedAt && conversation.selectionIntent && !conversation.selectedThread
        ? <p role="alert">{t("child.unavailable")}</p> : null}
      {conversation.selectedThread ? <ChildThreadConversation thread={conversation.selectedThread} conversation={conversation}
        onFork={onForkFromThread ? forkSelectedThread : undefined} /> : null}
    </section>
  );
}

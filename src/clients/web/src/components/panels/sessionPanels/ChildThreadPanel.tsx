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
 * 主窗口右侧侧边栏「运行与连接」标签中的子会话线程面板。
 * 这是会话层级资源：展示当前会话委派的 child thread，
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
        setNotice(`已复制 child thread ID: ${threadId}`);
      })
      .catch((copyError: unknown) => {
        setNoticeError(true);
        setNotice(
          `复制失败: ${
            errorMessage(copyError)
          }`,
        );
      });
  };

  return (
    <section className="panel-view child-thread-panel" aria-label="子会话线程">
      <div className="panel-header">
        <div
          className="panel-title"
          title={`${sessionId || "无会话"}${loadedAt ? ` · 最近读取 ${loadedAt}` : ""}`}
        >
          子会话线程 <span className="resource-total-count">{threads.length}</span>
        </div>
        <div className="panel-header-meta">
          {total > threads.length ? <span>后端共 {total} 条</span> : null}
        </div>
        <button
          type="button"
          className="resource-icon-button"
          onClick={onRefresh}
          disabled={loading || !sessionId}
          title="刷新子会话线程"
          aria-label="刷新子会话线程"
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
              aria-label="关闭提示"
              onClick={() => setNotice("")}
            >
              <span className="codicon codicon-close" aria-hidden="true" />
            </button>
          </div>
        ) : null}
      </div>

      {loading ? <div className="empty-state">正在读取子会话线程...</div> : null}
      {!loading && error ? (
        <div className="empty-state">子会话线程加载失败：{error}</div>
      ) : null}
      {!loading && !error && threads.length === 0 && teams.length === 0 && sessionId ? (
        <div className="empty-state">
          当前会话还没有委托子会话；Agent 委派 subagent 后，这里会显示对应的子会话线程。
        </div>
      ) : null}
      {!loading && !error && threads.length === 0 && !sessionId ? (
        <div className="empty-state">选择会话后查看其子会话线程。</div>
      ) : null}

      {!loading && !error && (threads.length > 0 || Boolean(sessionId)) ? (
        <div className="child-thread-list" role="list">
          {teams.map((team) => (
            <article key={team.team_id} className="child-thread-item" role="listitem" data-team-id={team.team_id} data-team-version={team.version}>
              <div className="child-thread-copy"><strong>{team.name}</strong><small>团队版本 {team.version}</small></div>
              <div className="child-thread-job-status">协调线程：{team.coordinator_thread_id}</div>
              {team.members.map((member) => (
                <div key={member.thread_id} className="child-thread-runtime" data-team-member-thread-id={member.thread_id}>
                  {member.role} · {member.work_mode === "read_only" ? "只读" : "可写"} · {member.thread_id}
                </div>
              ))}
              {team.tasks.map((task) => (
                <div key={task.task_id} className="child-thread-runtime" data-team-task-id={task.task_id} data-team-task-status={task.status}>
                  <strong>{task.title}</strong> · {{ queued: "等待执行", in_progress: "进行中", blocked: "受阻", completed: "已完成", failed: "失败", cancelled: "已取消" }[task.status as "queued" | "in_progress" | "blocked" | "completed" | "failed" | "cancelled"]}
                  <div>执行线程：{task.assignee_thread_id} · {task.phase} · 第 {task.cycle} 轮</div>
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
                    if (mainThreadId) onSelectThread?.(mainThreadId);
                  }}
                  disabled={!mainThreadId || !onSelectThread}
                  title="切换调试 owner：主线程"
                  aria-label="切换到主线程调试"
                >
                  <span
                    className="child-thread-icon codicon codicon-home"
                    aria-hidden="true"
                  />
                  <span className="child-thread-copy">
                    <strong>主线程</strong>
                    <small>main · 当前会话默认调试 owner</small>
                  </span>
                  <span className="child-thread-status child-thread-status-running">
                    {mainThreadId !== null && mainThreadId === activeThreadId
                      ? "当前调试"
                      : "主线程"}
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
                    onClick={() => conversation.selectThread(thread.thread_id)}
                    title={`打开子线程对话：${thread.title ?? thread.thread_id}`}
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
                  {onSelectThread ? <button type="button" className="child-thread-copy-id" onClick={() => onSelectThread(thread.thread_id)} aria-label={`调试子线程：${thread.thread_id}`} title={isCurrent ? "当前调试 owner" : "调试此子线程"}>
                    <span className="codicon codicon-debug" aria-hidden="true" />
                  </button> : null}
                  <button
                    type="button"
                    className="child-thread-copy-id"
                    onClick={() => handleCopyThreadId(thread.thread_id)}
                    title={`复制 child thread ID: ${thread.thread_id}`}
                    aria-label={`复制 child thread ID: ${thread.thread_id}`}
                  >
                    <span className="codicon codicon-copy" aria-hidden="true" />
                  </button>
                </div>
                {thread.runtime ? (
                  <div className="child-thread-runtime">
                    {{ cold: "运行时已释放", loading: "正在装载运行时", resident: "运行时驻留", unloading: "正在释放运行时" }[thread.runtime.residency]}
                    {thread.runtime.cold_eligible ? " · 可释放" : ""}
                    {thread.runtime.last_activity_at ? ` · 最近活动 ${formatDateTime(thread.runtime.last_activity_at)}` : ""}
                    {thread.runtime.idle_deadline ? ` · 空闲释放时间 ${formatDateTime(thread.runtime.idle_deadline)}` : ""}
                    {thread.runtime.blockers.map((blocker) => <div key={blocker.kind}>{blocker.reason}</div>)}
                  </div>
                ) : null}
                {thread.report_execution_id && thread.report_status && thread.report_status !== "unknown" ? (
                  <button type="button" className="child-thread-report-link" onClick={() => conversation.selectThread(thread.thread_id, "report")}>
                    {conversation.isUnread(thread) ? "新报告 · " : ""}查看子线程报告
                  </button>
                ) : null}
                {thread.admission_state || thread.collaboration_state ? (
                  <div className="child-thread-job-status">
                    admission: {thread.admission_state ?? "未知"} · collaboration: {thread.collaboration_state ?? "未知"}
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : null}
      {!loading && !error && loadedAt && conversation.selectionIntent && !conversation.selectedThread
        ? <p role="alert">所选子线程已不可用，无法读取其历史或报告。</p> : null}
      {conversation.selectedThread ? <ChildThreadConversation thread={conversation.selectedThread} conversation={conversation}
        onFork={onForkFromThread ? forkSelectedThread : undefined} /> : null}
    </section>
  );
}

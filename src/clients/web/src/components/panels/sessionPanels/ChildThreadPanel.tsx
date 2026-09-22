import { useState } from "react";
import type { ChildThreadSummary } from "../../../types/backend";
import { copyTextToClipboard } from "../../../utils/clipboard";
import { formatDateTime } from "../../../utils/format";
import {
  childThreadStatusClass,
  childThreadStatusLabel,
} from "../../../state/childThreadDisplay";

/** child thread 在列表中的图标；用分支语义表达“从 owner 会话派生”。 */
const CHILD_THREAD_ICON = "codicon-git-branch";

/**
 * 主窗口右侧侧边栏「运行与连接」标签中的子会话线程面板。
 * 这是会话层级资源：展示当前会话委派的 child thread，
 * 数据由 App 层加载后通过 props 传入，组件本身不持有业务状态权威。
 */
export default function ChildThreadPanel({
  threads,
  total,
  loading,
  error,
  loadedAt,
  sessionId,
  activeThreadId,
  onRefresh,
  onSelectThread,
}: {
  threads: ChildThreadSummary[];
  total: number;
  loading: boolean;
  error: string | null;
  loadedAt: string | null;
  sessionId: string;
  /** 当前调试 owner 的 thread id。 */
  activeThreadId: string;
  onRefresh: () => void;
  onSelectThread: (threadId: string) => void;
}) {
  const [notice, setNotice] = useState("");
  const [noticeError, setNoticeError] = useState(false);

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
            copyError instanceof Error ? copyError.message : String(copyError)
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

      {notice ? (
        <div
          className={`child-thread-notice${noticeError ? " is-error" : ""}`}
          role="status"
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

      {loading ? <div className="empty-state">正在读取子会话线程...</div> : null}
      {!loading && error ? (
        <div className="empty-state">子会话线程加载失败：{error}</div>
      ) : null}
      {!loading && !error && threads.length === 0 && sessionId ? (
        <div className="empty-state">
          当前会话还没有委托子会话；Agent 委派 subagent 后，这里会显示对应的子会话线程。
        </div>
      ) : null}
      {!loading && !error && threads.length === 0 && !sessionId ? (
        <div className="empty-state">选择会话后查看其子会话线程。</div>
      ) : null}

      {!loading && !error && (threads.length > 0 || Boolean(sessionId)) ? (
        <div className="child-thread-list" role="list">
          {sessionId ? (
            <article
              className="child-thread-item child-thread-owner-item"
              role="listitem"
              data-thread-id="main"
            >
              <div className="child-thread-row">
                <button
                  type="button"
                  className="child-thread-main"
                  onClick={() => onSelectThread("main")}
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
                    {activeThreadId === "main" ? "当前调试" : "主线程"}
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
              >
                <div className="child-thread-row">
                  <button
                    type="button"
                    className="child-thread-main"
                    onClick={() => onSelectThread(thread.thread_id)}
                    title={`切换调试 owner：${thread.title ?? thread.thread_id}`}
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
                      {isCurrent ? "当前调试" : childThreadStatusLabel(thread.status)}
                    </span>
                  </button>
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
    </section>
  );
}

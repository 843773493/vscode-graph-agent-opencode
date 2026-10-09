import type { ChildThreadSummary, InternalExecutionHistory } from "../../../types/backend";
import type { useChildThreadConversation } from "../../../hooks/sessionTurnHistory/useChildThreadConversation";
import MarkdownContent from "../../chat/MarkdownContent";

type Conversation = ReturnType<typeof useChildThreadConversation>;

function Report({ execution }: { execution: InternalExecutionHistory }) {
  return (
    <article className="child-thread-report" data-execution-id={execution.execution_id}>
      <strong>子线程报告 · {execution.status}</strong>
      {execution.display_parts.map((part) => <MarkdownContent key={part.item_id} value={part.text} />)}
      {execution.response_parts.filter((part) => part.kind === "text" || part.kind === "final_text").map((part) => <MarkdownContent key={part.part_id} value={part.text ?? ""} />)}
      {execution.display_parts.length === 0 && execution.response_parts.length === 0 ? <p>此执行没有文本报告。</p> : null}
    </article>
  );
}

/** 主窗口右侧侧边栏中的精确子线程对话；不修改主线程聊天选择。 */
export default function ChildThreadConversation({ thread, conversation }: { thread: ChildThreadSummary; conversation: Conversation }) {
  const { snapshot } = conversation;
  const page = snapshot[snapshot.view];
  return (
    <section className="child-thread-conversation" aria-label={`子线程对话：${thread.title ?? thread.thread_id}`} data-thread-id={thread.thread_id}>
      <div className="child-thread-conversation-header">
        <strong>{thread.title ?? "子线程对话"}</strong>
        <button type="button" onClick={conversation.close} aria-label="关闭子线程对话">关闭</button>
      </div>
      <div className="child-thread-conversation-actions">
        <button type="button" onClick={() => void conversation.refresh()} disabled={snapshot.loading}>最新对话</button>
        <button type="button" onClick={() => void conversation.openReport()} disabled={snapshot.loading || !thread.report_execution_id || !thread.report_status || thread.report_status === "unknown"}>查看报告</button>
        {page?.has_before ? <button type="button" onClick={() => void conversation.loadEarlier()} disabled={snapshot.loading}>更早记录</button> : null}
      </div>
      {snapshot.error ? <p className="child-thread-error" role="alert">{snapshot.error}</p> : null}
      {snapshot.loading ? <p role="status">正在读取子线程{snapshot.view === "report" ? "报告" : "历史"}…</p> : null}
      {snapshot.accepted ? <p role="status">消息已接收，任务 {snapshot.accepted.job_id}</p> : null}
      <div className="child-thread-conversation-history">
        {(page?.internal_executions ?? []).map((execution) => <Report key={execution.execution_id} execution={execution} />)}
        {(page?.items ?? []).map((turn) => (
          <article key={turn.turn_id} className="child-thread-turn" data-turn-id={turn.turn_id}>
            <small>对话 · {turn.status}</small>
            {(turn.user_messages ?? []).map((message) => <div key={message.message_id} className="child-thread-user"><strong>你</strong><MarkdownContent value={message.content} /></div>)}
            <div className="child-thread-assistant"><strong>Agent</strong><MarkdownContent value={turn.final_response || (turn.assistant_text ?? []).join("\n\n") || turn.response_preview || "等待回复…"} /></div>
            {turn.detail_truncated ? <p>当前记录达到历史读取预算；可在调试视图查看执行详情。</p> : null}
          </article>
        ))}
        {page && page.items.length === 0 && (page.internal_executions ?? []).length === 0 && !snapshot.loading ? <p>该子线程还没有已提交的对话记录。</p> : null}
      </div>
      <form onSubmit={(event) => { event.preventDefault(); void conversation.send(); }}>
        <label htmlFor={`child-thread-draft-${thread.thread_id}`}>向该子线程发送消息</label>
        <textarea id={`child-thread-draft-${thread.thread_id}`} value={snapshot.draft} onChange={(event) => conversation.setDraft(event.target.value)} rows={3} disabled={snapshot.sending} />
        <button type="submit" disabled={snapshot.sending || !snapshot.draft.trim()}>{snapshot.sending ? "正在发送…" : "发送到子线程"}</button>
      </form>
    </section>
  );
}

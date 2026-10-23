import { memo, useMemo } from "react";
import type { ChildThreadSummary, InternalExecutionHistory, TurnDetail, TurnHistoryPage } from "../../../types/backend";
import type { useChildThreadConversation } from "../../../hooks/sessionTurnHistory/useChildThreadConversation";
import type { ChatTurnHandlers } from "../../chat/turn/types";
import { conversationFromTurn } from "../../../state/conversations/turnProjection";
import { buildRenderGroups, responseItemsForConversation } from "../../../state/timeline/chatResponseGroups";
import MarkdownContent from "../../chat/MarkdownContent";
import { TurnActivitySummary } from "../../chat/turn/TurnActivitySummary";

type Conversation = ReturnType<typeof useChildThreadConversation>;
type ChatTurnForkPoint = Parameters<NonNullable<ChatTurnHandlers["onForkConversation"]>>[0];
export type ChildThreadForkPoint = Partial<Pick<ChatTurnForkPoint,
  "sourceViewId" | "sourceViewRevision" | "turnId" | "itemId">>;

const Report = memo(function Report({ execution }: { execution: InternalExecutionHistory }) {
  const textParts = useMemo(
    () => execution.response_parts.filter((part) => part.kind === "text" || part.kind === "final_text"),
    [execution.response_parts],
  );
  return (
    <article className="child-thread-report" data-execution-id={execution.execution_id}>
      <strong>子线程报告 · {execution.status}</strong>
      {execution.display_parts.map((part) => <MarkdownContent key={part.item_id} value={part.text} />)}
      {textParts.map((part) => <MarkdownContent key={part.part_id} value={part.text ?? ""} />)}
      {execution.display_parts.length === 0 && execution.response_parts.length === 0 ? <p>此执行没有文本报告。</p> : null}
    </article>
  );
});

type ForkPoint = (point: ChildThreadForkPoint) => void;

const ChildTurn = memo(function ChildTurn({ turn, threadId, onFork }: { turn: TurnDetail; threadId: string; onFork?: ForkPoint }) {
  const conversation = conversationFromTurn(turn, threadId);
  const groups = buildRenderGroups(responseItemsForConversation(conversation));
  const sourceSnapshot = { sourceViewId: turn.source_view_id, sourceViewRevision: turn.source_view_revision };
  return (
    <article className="child-thread-turn chat-turn" data-turn-id={turn.turn_id} data-conversation-id={turn.turn_id} data-root-logical-item-ordinal={turn.root_logical_item_ordinal}>
      <small>对话 · {turn.status}</small>
      {(turn.user_messages ?? []).map((message) => <div key={message.message_id} className="child-thread-user chat-user-text"><strong>你</strong><MarkdownContent value={message.content} />
        {onFork ? <button type="button" onClick={() => onFork({ ...sourceSnapshot, turnId: turn.turn_id })}>从此消息创建分支</button> : null}
      </div>)}
      <div className="child-thread-assistant chat-assistant-content">
        <TurnActivitySummary conversation={conversation} groups={groups} showRawDetails={false}
          onForkItem={onFork ? (itemId) => onFork({ ...sourceSnapshot, itemId }) : undefined} />
      </div>
      {turn.detail_truncated ? <p>当前记录达到历史读取预算；可在调试视图查看执行详情。</p> : null}
    </article>
  );
});

function firstVisibleOrdinalForExecution(execution: InternalExecutionHistory): number {
  let firstOrdinal = Infinity;
  for (const part of execution.display_parts) {
    firstOrdinal = Math.min(firstOrdinal, part.logical_item_ordinal);
  }
  for (const part of execution.response_parts) {
    firstOrdinal = Math.min(firstOrdinal, part.source.logical_item_ordinal ?? Infinity);
  }
  return firstOrdinal;
}

const ChildConversationTimeline = memo(function ChildConversationTimeline({
  page,
  threadId,
  loading,
  onFork,
}: {
  page: TurnHistoryPage | null;
  threadId: string;
  loading: boolean;
  onFork?: ForkPoint;
}) {
  const entries = useMemo(() => [
    ...(page?.internal_executions ?? []).map((execution) => ({
      ordinal: firstVisibleOrdinalForExecution(execution),
      key: execution.execution_id,
      node: <Report execution={execution} />,
    })),
    ...(page?.items ?? []).map((turn) => ({
      ordinal: turn.root_logical_item_ordinal,
      key: turn.turn_id,
      node: <ChildTurn turn={turn} threadId={threadId} onFork={onFork} />,
    })),
  ].sort((left, right) => left.ordinal - right.ordinal), [onFork, page, threadId]);

  return (
    <div className="child-thread-conversation-history">
      {entries.map((entry) => <div key={entry.key}>{entry.node}</div>)}
      {page && page.items.length === 0 && (page.internal_executions ?? []).length === 0 && !loading
        ? <p>该子线程还没有已提交的对话记录。</p>
        : null}
    </div>
  );
});

/** 主窗口右侧侧边栏中的精确子线程对话；不修改主线程聊天选择。 */
export default function ChildThreadConversation({ thread, conversation, onFork }: { thread: ChildThreadSummary; conversation: Conversation; onFork?: ForkPoint }) {
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
        {onFork ? <button type="button" onClick={() => onFork({})}>从此线程创建分支</button> : null}
        {page?.has_before ? <button type="button" onClick={() => void conversation.loadEarlier()} disabled={snapshot.loading}>更早记录</button> : null}
      </div>
      {snapshot.error ? <p className="child-thread-error" role="alert">{snapshot.error}</p> : null}
      {snapshot.loading ? <p role="status">正在读取子线程{snapshot.view === "report" ? "报告" : "历史"}…</p> : null}
      {snapshot.accepted ? <p role="status">消息已接收，任务 {snapshot.accepted.job_id}</p> : null}
      <ChildConversationTimeline page={page} threadId={thread.thread_id} loading={snapshot.loading} onFork={onFork} />
      <form onSubmit={(event) => { event.preventDefault(); void conversation.send(); }}>
        <label htmlFor={`child-thread-draft-${thread.thread_id}`}>向该子线程发送消息</label>
        <textarea id={`child-thread-draft-${thread.thread_id}`} value={snapshot.draft} onChange={(event) => conversation.setDraft(event.target.value)} rows={3} disabled={snapshot.sending} />
        <button type="submit" disabled={snapshot.sending || !snapshot.draft.trim()}>{snapshot.sending ? "正在发送…" : "发送到子线程"}</button>
      </form>
    </section>
  );
}

import { t } from "../../../../../shared/core/i18n/index.js";
import { memo, useMemo } from "react";
import type { ChildThreadSummary, InternalExecutionHistory, TurnDetail, TurnHistoryPage } from "../../../types/backend";
import type { useChildThreadConversation } from "../../../hooks/sessionTurnHistory/useChildThreadConversation";
import type { ChatTurnHandlers } from "../../chat/turn/types";
import { conversationModelUsage, conversationTokenUsage } from "../../../state/tokenUsage";
import { conversationFromTurn } from "../../../state/conversations/turnProjection";
import { buildRenderGroups, responseItemsForConversation } from "../../../state/timeline/chatResponseGroups";
import MarkdownContent from "../../chat/MarkdownContent";
import { TurnActivitySummary } from "../../chat/turn/TurnActivitySummary";
import { ChatItemOwnerContext, copyItemAction, useChatItemMenu } from "../../chat/itemContextMenu";
import MessageSubmissionRecovery from "../../composer/MessageSubmissionRecovery";
import ContextNotice from "../../chat/ContextNotice";
import { responsePartsToTimelineItems } from "../../../state/timeline/responseParts";

import { childExecutionStatusLabel } from "../../../state/childThreadDisplay";

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
      <strong>{t("child.reportStatus", childExecutionStatusLabel(execution.status))}</strong>
      {responsePartsToTimelineItems(execution.response_parts).map(item => item.kind === "context_event"
        ? <ContextNotice key={item.id} notices={item.notices} /> : null)}
      {execution.display_parts.map((part) => <MarkdownContent key={part.item_id} value={part.text} />)}
      {textParts.map((part) => <MarkdownContent key={part.part_id} value={part.text ?? ""} />)}
      {execution.display_parts.length === 0 && execution.response_parts.length === 0 ? <p>{t("child.noReport")}</p> : null}
    </article>
  );
});

type ForkPoint = (point: ChildThreadForkPoint) => void;

function ChildUserMessage({ message, turn, onFork }: {
  message: NonNullable<TurnDetail["user_messages"]>[number]; turn: TurnDetail; onFork?: ForkPoint;
}) {
  const menu = useChatItemMenu(() => [copyItemAction(message.content), {
    id: "forkItem", label: t("chat.itemMenu.fork"), tooltip: t("chat.itemMenu.forkHint"), enabled: Boolean(onFork),
    run: () => onFork?.({ sourceViewId: turn.source_view_id, sourceViewRevision: turn.source_view_revision, turnId: turn.turn_id }),
  }], true, () => ({ messageId: message.message_id }));
  return <div className="child-thread-user chat-user-text" {...menu}>
    <strong>{t("child.you")}</strong><MarkdownContent value={message.content} />
  </div>;
}

const ChildTurn = memo(function ChildTurn({ turn, threadId, onFork }: { turn: TurnDetail; threadId: string; onFork?: ForkPoint }) {
  const conversation = conversationFromTurn(turn, threadId);
  const groups = buildRenderGroups(responseItemsForConversation(conversation));
  const tokenUsage = conversationTokenUsage(conversation);
  const modelUsage = conversationModelUsage(conversation);
  const sourceSnapshot = { sourceViewId: turn.source_view_id, sourceViewRevision: turn.source_view_revision };
  return (
    <ChatItemOwnerContext.Provider value={{ sessionId: turn.session_id, threadId }}><article className="child-thread-turn chat-turn" data-turn-id={turn.turn_id} data-conversation-id={turn.turn_id} data-root-logical-item-ordinal={turn.root_logical_item_ordinal}>
      <small>{t("child.turnStatus", childExecutionStatusLabel(turn.status))}</small>
      {(turn.user_messages ?? []).map((message) => <ChildUserMessage key={message.message_id} message={message} turn={turn} onFork={onFork} />)}
      <div className="child-thread-assistant chat-assistant-content">
        <TurnActivitySummary conversation={conversation} groups={groups} showRawDetails={false}
          usage={tokenUsage || modelUsage ? { tokenUsage, modelUsage } : undefined}
          onForkItem={onFork ? (itemId) => onFork({ ...sourceSnapshot, itemId }) : undefined} />
      </div>
      {turn.detail_truncated ? <p>{t("child.historyBudget")}</p> : null}
    </article></ChatItemOwnerContext.Provider>
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
        ? <p>{t("child.emptyHistory")}</p>
        : null}
    </div>
  );
});

/** 主窗口右侧侧边栏中的精确子线程对话；不修改主线程聊天选择。 */
export default function ChildThreadConversation({ thread, conversation, onFork }: { thread: ChildThreadSummary; conversation: Conversation; onFork?: ForkPoint }) {
  const { snapshot } = conversation;
  const page = snapshot[snapshot.view];
  return (
    <section className="child-thread-conversation panel-feedback-host" aria-busy={snapshot.loading} aria-label={t("child.conversationAria", thread.title ?? thread.thread_id)} data-thread-id={thread.thread_id}>
      <div className="child-thread-conversation-header">
        <strong>{thread.title ?? t("child.conversation")}</strong>
        <button type="button" onClick={conversation.close} aria-label={t("child.closeAria")}>{t("child.close")}</button>
      </div>
      <div className="child-thread-conversation-actions">
        <button type="button" onClick={() => void conversation.refresh()} disabled={snapshot.loading}><span className={`codicon codicon-refresh${snapshot.loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />{t("child.latest")}</button>
        <button type="button" onClick={() => void conversation.openReport()} disabled={snapshot.loading || !thread.report_execution_id || !thread.report_status || thread.report_status === "unknown"}>{t("child.viewReport")}</button>
        {onFork ? <button type="button" onClick={() => onFork({})}>{t("child.fork")}</button> : null}
        {page?.has_before ? <button type="button" onClick={() => void conversation.loadEarlier()} disabled={snapshot.loading}>{t("child.earlier")}</button> : null}
      </div>
      {snapshot.error ? <p className="panel-feedback" role="alert">{snapshot.error}</p> : null}


      <ChatItemOwnerContext.Provider value={conversation.owner}>
        <ChildConversationTimeline page={page} threadId={thread.thread_id} loading={snapshot.loading} onFork={onFork} />
      </ChatItemOwnerContext.Provider>
      {conversation.submissionRecovery && <MessageSubmissionRecovery {...conversation.submissionRecovery}
        onAccepted={() => { void conversation.refresh(); }} />}
      <form onSubmit={(event) => { event.preventDefault(); void conversation.send(); }}>
        <label htmlFor={`child-thread-draft-${thread.thread_id}`}>{t("child.messageLabel")}</label>
        <textarea id={`child-thread-draft-${thread.thread_id}`} value={snapshot.draft} onChange={(event) => conversation.setDraft(event.target.value)} rows={3} disabled={snapshot.sending} />
        <button type="submit" title={snapshot.accepted ? t("child.accepted", snapshot.accepted.job_id) : undefined} disabled={snapshot.sending || !snapshot.draft.trim()}>{snapshot.sending ? t("child.sending") : t("child.send")}</button>
      </form>
    </section>
  );
}

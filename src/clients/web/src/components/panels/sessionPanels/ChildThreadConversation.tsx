import { t } from "../../../../../shared/core/i18n/index.js";
import { memo, useCallback, useMemo } from "react";
import { Virtuoso } from "react-virtuoso";
import { chatTranscriptItems } from "../../chat/transcriptItems";
import { useTurnBodyWindow } from "../../chat/useTurnBodyWindow";
import type { ExecutionItemReference } from "../../../state/session/internalExecutionHistory";
import type { ChildThreadHistoryPage } from "../../../state/childThreadDisplay";
import type { ChildThreadSummary, TurnDetail } from "../../../types/backend";
import type { useChildThreadConversation } from "../../../hooks/sessionTurnHistory/useChildThreadConversation";
import type { ChatTurnHandlers } from "../../chat/turn/types";
import { conversationModelUsage, conversationTokenUsage } from "../../../state/tokenUsage";
import { conversationFromTurn } from "../../../state/conversations/turnProjection";
import { buildRenderGroups, responseItemsForConversation } from "../../../state/timeline/chatResponseGroups";
import MarkdownContent from "../../chat/MarkdownContent";
import { TurnActivitySummary } from "../../chat/turn/TurnActivitySummary";
import { ProcessActivity } from "../../chat/turn/TurnActivitySummary";
import { TranscriptViewContext, useTranscriptView } from "../../chat/TranscriptViewContext";
import { responsePartsToTimelineItems } from "../../../state/timeline/responseParts";
import type { LoadTurnDetails } from "../../chat/turn/types";
import { ChatItemOwnerContext, copyItemAction, useChatItemMenu } from "../../chat/itemContextMenu";
import MessageSubmissionRecovery from "../../composer/MessageSubmissionRecovery";
import ContextNotice from "../../chat/ContextNotice";

import { childExecutionStatusLabel } from "../../../state/childThreadDisplay";

type Conversation = ReturnType<typeof useChildThreadConversation>;
type ChatTurnForkPoint = Parameters<NonNullable<ChatTurnHandlers["onForkConversation"]>>[0];
export type ChildThreadForkPoint = Partial<Pick<ChatTurnForkPoint,
  "sourceViewId" | "sourceViewRevision" | "turnId" | "itemId">>;

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

const ChildTurn = memo(function ChildTurn({ turn, threadId, onFork, loadDetails }: { turn: ChildThreadHistoryPage["items"][number]; threadId: string; onFork?: ForkPoint; loadDetails: LoadTurnDetails }) {
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
          onLoadTurnDetails={loadDetails}
          usage={tokenUsage || modelUsage ? { tokenUsage, modelUsage } : undefined}
          onForkItem={onFork ? (itemId) => onFork({ ...sourceSnapshot, itemId }) : undefined} />
      </div>
      {turn.detail_truncated ? <p>{t("child.historyBudget")}</p> : null}
    </article></ChatItemOwnerContext.Provider>
  );
});

const ChildConversationTimeline = memo(function ChildConversationTimeline({
  page, threadId, loading, onFork, scopeKey, retainBodies, onError, loadDetails,
}: {
  page: ChildThreadHistoryPage | null;
  threadId: string;
  loading: boolean;
  onFork?: ForkPoint;
  scopeKey: string;
  retainBodies: (turnIds: string[], items?: ExecutionItemReference[]) => Promise<void>;
  onError: (error: unknown) => void;
  loadDetails: LoadTurnDetails;
}) {
  const {transcriptView} = useTranscriptView(scopeKey);
  const items = useMemo(() => chatTranscriptItems(
    (page?.items ?? []).map(turn => conversationFromTurn(turn, threadId)),
    page?.internal_executions ?? [], true,
  ), [page, threadId]);
  const rendered = useCallback(() => {}, []);
  const { heights, itemsRendered } = useTurnBodyWindow({ items, scopeKey,
    retain: retainBodies, onError, onRendered: rendered });
  if (items.length === 0) return <div className="child-thread-conversation-history">
    {page && !loading ? <p>{t("child.emptyHistory")}</p> : null}
  </div>;
  return <TranscriptViewContext.Provider value={transcriptView}><Virtuoso className="child-thread-conversation-history" style={{ height: 420 }}
    data={items} initialItemCount={Math.min(items.length, 4)} computeItemKey={(_, item) => item.key} itemsRendered={itemsRendered}
    itemContent={(_, item) => {
      if (item.kind === "turn") {
        const turn = page?.items.find(turn => turn.turn_id === item.conversation.turnId);
        return turn ? <ChildTurn turn={turn} threadId={threadId} onFork={onFork} loadDetails={loadDetails} /> : null;
      }
      if (item.kind === "context_event") return <ContextNotice notices={item.notices} />;
      if (item.kind === "internal_execution") return item.displayPart.bodyEvicted
        ? <div style={{ height: heights.get(item.key) }} aria-busy="true" role="status">{t("chat.turn.loadingFull")}</div>
        : <MarkdownContent value={item.displayPart.text} />;
      const execution = page?.internal_executions?.find(execution => execution.execution_id === item.executionId);
      if (!execution) throw new Error("子 Thread 展示段缺少 execution");
      return <ProcessActivity process={{kind:"execution",id:item.executionId}} settled
        segment={item.kind === "execution_response" ? item.itemId : undefined}
        groups={item.kind === "execution_response" ? buildRenderGroups(responsePartsToTimelineItems(item.parts)) : []}
        status={{title:childExecutionStatusLabel(execution.status),icon:execution.failure ? "codicon-error" : "codicon-check",detail:execution.failure?.message}}
        outline={execution.activityOutline} loadDetails={loadDetails} showRawDetails={false}
        binding={page?.source_view_id && page.source_view_revision !== undefined ? {sourceViewId:page.source_view_id,sourceViewRevision:page.source_view_revision}:undefined} />;
    }} /></TranscriptViewContext.Provider>;
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
        <ChildConversationTimeline page={page} threadId={thread.thread_id} loading={snapshot.loading} onFork={onFork}
          scopeKey={JSON.stringify([conversation.owner, snapshot.view])} retainBodies={conversation.retainExecutionBodies}
          loadDetails={conversation.loadProcessDetails}
          onError={conversation.reportHistoryError} />
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

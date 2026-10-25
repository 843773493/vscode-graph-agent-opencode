import React from "react";
import { conversationModelUsage, conversationTokenUsage } from "../../../state/tokenUsage";
import { buildPendingStatusItem, isLiveConversationView } from "../../../state/trace/traceAggregation";
import type { TimelineItem } from "../../../state/timeline/timelineTypes";
import { buildRenderGroups, responseItemsForConversation } from "../../../state/timeline/chatResponseGroups";
import type { ConversationView } from "../../../types/frontend";
import ResponseActionToolbar from "../ResponseActionToolbar";
import type { ChatTurnActions } from "./useChatTurnActions";
import type { LoadToolDetails, LoadTurnDetails } from "./types";
import { RewindStatusPart, MessageStreamStatusPart, HistoricalBoundaryStatusPart, ExecutionLostRecoveryPart, ResponsePart } from "./responseStatusParts";
import { ConversationWorkSummary, ForkItemButtons, TurnActivitySummary } from "./TurnActivitySummary";
import type { ChatTurnHandlers } from "./types";

export default function ChatTurnResponseBody({
  conversation,
  workspaceId,
  showRawDetails,
  actions,
  onLoadTurnDetails,
  onLoadToolDetails,
  onForkConversation,
}: {
  conversation: ConversationView;
  workspaceId?: string | null;
  showRawDetails: boolean;
  actions: ChatTurnActions;
  onLoadTurnDetails?: LoadTurnDetails;
  onLoadToolDetails?: LoadToolDetails;
  onForkConversation?: ChatTurnHandlers["onForkConversation"];
}): React.ReactNode {
  const running = isLiveConversationView(conversation)
    && (conversation.status === "running" || conversation.status === "queued");
  const summaryOnly = conversation.turnItemsView === "summary";
  const hasPersistedResponse = (conversation.responseParts?.length ?? 0) > 0
    || (conversation.assistantMessages?.length ?? 0) > 0;
  const hasUnifiedParts = !running && hasPersistedResponse;
  // 实时回答只来自 message.v1；旧 Trace 仍可在事件/请求视图查看，
  // 但不能在聊天主线作为静默兼容回退，避免两套语义互相覆盖。
  const parts = hasUnifiedParts || Boolean(conversation.messageStream)
    ? responseItemsForConversation(conversation)
    : [];
  const hasSessionInterrupted = parts.some((item) =>
    item.kind === "trace" && item.eventType === "session_interrupted"
  );
  const visibleParts = parts.filter((item) =>
    item.kind === "aggregated_text"
    || item.kind === "aggregated_tool"
    || (item.kind === "trace"
      && ["error", "job_failed", "job_cancelled", "session_interrupted"]
        .includes(item.eventType)
      && !(hasSessionInterrupted && item.eventType === "job_cancelled")),
  );
  const renderGroups = buildRenderGroups(visibleParts);
  const activityItems = renderGroups.flatMap((group) => group.kind === "work" ? group.items : []);
  const historyTurn = conversation.displayMode === "history" && Boolean(conversation.turnId);
  const onForkItem = conversation.displayMode === "history" && onForkConversation
    ? (itemId: string) => {
      if (!workspaceId || !conversation.threadId || !conversation.sourceViewId || conversation.sourceViewRevision === undefined) {
        throw new Error("历史消息缺少实际读取的来源快照");
      }
      onForkConversation({
        workspaceId, sessionId: conversation.sessionId, threadId: conversation.threadId,
        sourceViewId: conversation.sourceViewId, sourceViewRevision: conversation.sourceViewRevision, itemId,
      });
    } : undefined;
  const forkActions = onForkItem
    ? renderGroups.flatMap((group) => group.kind === "response"
      ? [<ForkItemButtons key={group.id} item={group.item} onForkItem={onForkItem} />]
      : [])
    : undefined;
  const showConversationWorkSummary = !historyTurn
    && (activityItems.length > 0 || (!running && Boolean(conversation.activityStats)));
  let responseTextPart: Extract<TimelineItem, { kind: "aggregated_text" }> | undefined;
  for (let index = visibleParts.length - 1; index >= 0; index -= 1) {
    const item = visibleParts[index];
    if (item?.kind === "aggregated_text" && item.partKind === "markdown") {
      responseTextPart = item;
      break;
    }
  }
  const hasActiveWork = visibleParts.some((item) =>
    (item.kind === "aggregated_tool"
      || (item.kind === "aggregated_text" && item.partKind === "reasoning"))
    && item.active,
  );
  const hasStreamingMarkdown = visibleParts.some((item) =>
    item.kind === "aggregated_text" && item.partKind === "markdown" && item.active
  );
  const status = running
    && Boolean(conversation.messageStream)
    && !hasActiveWork
    && !hasStreamingMarkdown
    && !showConversationWorkSummary
    ? buildPendingStatusItem(conversation)
    : null;
  const showResponseActions = !running && !conversation.pending;
  return (
    <>
      <RewindStatusPart conversation={conversation} />
      {historyTurn ? (
        <TurnActivitySummary
          conversation={conversation}
          groups={renderGroups}
          showRawDetails={showRawDetails}
          onLoadTurnDetails={onLoadTurnDetails}
          onLoadToolDetails={onLoadToolDetails}
          onForkItem={onForkItem}
        />
      ) : null}
      {showConversationWorkSummary ? (
        <ConversationWorkSummary
          conversation={conversation}
          items={activityItems}
          running={running}
        />
      ) : null}
      {!historyTurn ? renderGroups.map((group) => group.kind === "response" ? (
        <ResponsePart key={group.id} item={group.item} />
      ) : null) : null}
      {summaryOnly && !historyTurn ? (
        <div className="chat-turn-detail-loading" role="status">
          <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />
          <span>正在加载完整内容…</span>
        </div>
      ) : null}
      {status ? (
        <div className="chat-working" role="status">
          <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />
          <span>{status.title}</span>
          <span className="chat-working-detail">{status.detail}</span>
        </div>
      ) : null}
      <MessageStreamStatusPart conversation={conversation} />
      {!historyTurn ? <HistoricalBoundaryStatusPart conversation={conversation} /> : null}
      <ExecutionLostRecoveryPart
        conversation={conversation}
        actions={actions}
        running={running}
      />
      {showResponseActions ? (
        <ResponseActionToolbar
          responseText={responseTextPart?.text ?? ""}
          tokenUsage={conversationTokenUsage(conversation)}
          modelUsage={conversationModelUsage(conversation)}
          forkActions={forkActions}
        />
      ) : null}
      {actions.confirmAction ? (
        <div className="chat-turn-action-confirmation" role="group" aria-label="确认轮次操作">
          <div className="chat-turn-action-warning">
            将移除此消息之后的会话上下文，但不会撤销已产生的文件修改。
          </div>
          <div className="chat-request-edit-actions">
            <button
              type="button"
              disabled={actions.actionRunning}
              onClick={() => actions.setConfirmAction(null)}
            >
              取消
            </button>
            <button
              type="button"
              className="primary"
              disabled={actions.actionRunning}
              onClick={() => void actions.executeReplay(actions.confirmAction!)}
            >
              {actions.actionRunning
                ? "正在执行..."
                : actions.confirmAction === "regenerate"
                  ? "确认重新生成"
                  : "确认重试"}
            </button>
          </div>
        </div>
      ) : null}
      {actions.actionError ? (
        <div className="chat-inline-error" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>{actions.actionError}</span>
        </div>
      ) : null}
    </>
  );
}

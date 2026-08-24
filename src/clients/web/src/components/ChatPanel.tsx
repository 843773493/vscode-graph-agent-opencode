import React from "react";
import { Virtuoso } from "react-virtuoso";
import type { TurnHistoryInclude } from "../api/sessionTurnHistory";
import type {
  AttachmentRef,
  MessageReplayRequest,
  DeliveryPolicy,
  SessionChangesSummary,
  GatewayUserViewState,
} from "../types/backend";
import type { ConversationView } from "../types/frontend";
import { conversationTurnKey } from "../state/session/turnDetailHydration";
import type { TurnProjectionState } from "../state/session/turnTimeline";
import { isLiveConversationView } from "../state/trace/traceAggregation";
import ChatHistoryEmptyState from "./chat/ChatHistoryEmptyState";
import ChatHistoryPageHeader from "./chat/ChatHistoryPageHeader";
import ChatTurn from "./chat/ChatTurn";
import ChatTurnErrorBoundary from "./chat/ChatTurnErrorBoundary";
import { useTurnVirtualScroller } from "./chat/useTurnVirtualScroller";
import { useVisibleTurnDetailHydration } from "./chat/useVisibleTurnDetailHydration";

export function transcriptConversationsForDisplay(
  conversations: readonly ConversationView[],
): ConversationView[] {
  return conversations.filter((conversation) =>
    !conversation.pending || conversation.activeJobOverlay,
  );
}

export default function ChatPanel({
  apiPort,
  workspaceId,
  conversations,
  expandDetails,
  hasActiveSession,
  hasNewerMessages,
  hasOlderMessages,
  loadingNewerMessages,
  loadingOlderMessages,
  historyLoading,
  projectionState,
  timelineGeneration,
  projectionEpoch,
  historyError,
  onLoadAroundTurn,
  onLoadNewerMessages,
  onLoadOlderMessages,
  loadingDetailTurnIds,
  onLoadTurnDetails,
  onLoadToolDetails,
  onLoadAgentStateMessageRawContent,
  onRetryHistory,
  sessionChangeSummary,
  sessionChangesLoading,
  onOpenChanges,
  onReplayTurn,
  onUpdatePending,
  onRemovePending,
  onChangePendingPolicy,
  viewState,
  onViewStateChange,
  onViewStateRestoreStatus,
}: {
  apiPort: number;
  workspaceId?: string | null;
  conversations: ConversationView[];
  expandDetails: boolean;
  hasActiveSession: boolean;
  hasNewerMessages: boolean;
  hasOlderMessages: boolean;
  loadingNewerMessages: boolean;
  loadingOlderMessages: boolean;
  historyLoading: boolean;
  projectionState: TurnProjectionState;
  timelineGeneration: number;
  projectionEpoch: number | null;
  historyError: string | null;
  onLoadAroundTurn: (anchorTurnId: string) => Promise<void>;
  onLoadNewerMessages: () => Promise<void>;
  onLoadOlderMessages: () => Promise<void>;
  loadingDetailTurnIds: readonly string[];
  onLoadTurnDetails: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
    include?: TurnHistoryInclude[],
  ) => Promise<void>;
  onLoadToolDetails?: (turnId: string) => Promise<void>;
  onLoadAgentStateMessageRawContent: (
    sessionId: string,
    messageId: string,
  ) => Promise<string>;
  onRetryHistory: () => void;
  sessionChangeSummary?: SessionChangesSummary | null;
  sessionChangesLoading?: boolean;
  onOpenChanges?: () => void;
  onReplayTurn: (
    targetMessageId: string,
    action: MessageReplayRequest["action"],
    displayContent: string,
    content?: string,
    attachments?: AttachmentRef[],
  ) => Promise<void>;
  onUpdatePending: (
    messageId: string,
    content: string,
    attachments?: AttachmentRef[],
  ) => Promise<void>;
  onRemovePending: (messageId: string) => Promise<void>;
  onChangePendingPolicy: (
    messageId: string,
    policy: DeliveryPolicy,
    expectedSnapshotVersion?: number,
  ) => Promise<void>;
  viewState?: GatewayUserViewState | null;
  onViewStateChange?: (payload: {
    turn_anchor: string | null;
    scroll_offset: number;
    follow_latest: boolean;
  }) => void;
  onViewStateRestoreStatus?: (message: string) => void;
}): React.ReactNode {
  const [pendingActionError, setPendingActionError] = React.useState<string | null>(null);
  const [pendingActionRunning, setPendingActionRunning] = React.useState(false);
  const transcriptConversations = React.useMemo(
    () => transcriptConversationsForDisplay(conversations),
    [conversations],
  );
  const sessionId = transcriptConversations[0]?.sessionId
    ?? conversations[0]?.sessionId
    ?? "empty";
  const {
    bindScroller,
    firstItemIndex,
    followOutput,
    handleAtBottomChange,
    handleEndReached,
    handleStartReached,
    scrollToLatest,
    showJumpToLatest,
    streamRef,
  } = useTurnVirtualScroller({
    conversations: transcriptConversations,
    sessionId,
    onLoadNewerMessages,
    onLoadOlderMessages,
    onLoadAroundTurn,
    hasNewerMessages,
    hasOlderMessages,
    loadingNewerMessages,
    loadingOlderMessages,
    viewState,
    onViewStateChange,
    onViewStateRestoreStatus,
  });
  const {
    detailHydrationError,
    clearDetailHydrationError,
    hydrateVisibleTurns,
  } = useVisibleTurnDetailHydration({
    sessionId,
    timelineGeneration,
    projectionEpoch,
    conversations: transcriptConversations,
    firstItemIndex,
    loadingTurnIds: loadingDetailTurnIds,
    onLoadTurnDetails,
  });
  const sessionBusy = conversations.some(
    (conversation) => isLiveConversationView(conversation)
      && (conversation.status === "running" || conversation.status === "queued"),
  );
  const pendingRequests = conversations
    .filter((conversation) =>
      conversation.pending
      && !conversation.activeJobOverlay
      && conversation.userMessage
      && conversation.deliveryPolicy,
    )
    .map((conversation) => ({
      message_id: conversation.userMessage!.message_id,
      deliveryPolicy: conversation.deliveryPolicy!,
      enqueueSequence: conversation.enqueueSequence ?? Number.MAX_SAFE_INTEGER,
      waitingReason: conversation.waitingReason,
      snapshotVersion: conversation.queueSnapshotVersion,
    }))
    .sort((left, right) => left.enqueueSequence - right.enqueueSequence);

  const runPendingAction = React.useCallback(async (
    action: () => Promise<void>,
  ) => {
    if (pendingActionRunning) {
      return;
    }
    setPendingActionRunning(true);
    setPendingActionError(null);
    try {
      await action();
    } catch (error) {
      setPendingActionError(error instanceof Error ? error.message : String(error));
      throw error;
    } finally {
      setPendingActionRunning(false);
    }
  }, [pendingActionRunning]);

  const changePendingPolicy = React.useCallback(async (
    messageId: string,
    policy: DeliveryPolicy,
  ) => {
    const request = pendingRequests.find((item) => item.message_id === messageId);
    await onChangePendingPolicy(messageId, policy, request?.snapshotVersion);
  }, [onChangePendingPolicy, pendingRequests]);

  const updatePending = React.useCallback((
    messageId: string,
    content: string,
    attachments?: AttachmentRef[],
  ) => runPendingAction(
    () => onUpdatePending(messageId, content, attachments),
  ), [onUpdatePending, runPendingAction]);
  const removePending = React.useCallback((messageId: string) => runPendingAction(
    () => onRemovePending(messageId),
  ), [onRemovePending, runPendingAction]);
  const updatePendingPolicy = React.useCallback((
    messageId: string,
    policy: DeliveryPolicy,
  ) => runPendingAction(
    () => changePendingPolicy(messageId, policy),
  ), [changePendingPolicy, runPendingAction]);
  const retryHistory = React.useCallback(() => {
    clearDetailHydrationError();
    onRetryHistory();
  }, [clearDetailHydrationError, onRetryHistory]);

  return (
    <section className="chat-stream-shell">
      <section
        data-expand-details={String(expandDetails)}
        data-turn-count={transcriptConversations.length}
        data-first-turn-id={transcriptConversations[0] ? conversationTurnKey(transcriptConversations[0]) : ""}
        className="chat-stream-virtual-shell"
      >
      {transcriptConversations.length === 0 ? (
        hasActiveSession ? (
          <ChatHistoryEmptyState
            historyError={historyError}
            historyLoading={historyLoading}
            projectionState={projectionState}
            onRetryHistory={retryHistory}
            sessionChangeSummary={sessionChangeSummary}
            sessionChangesLoading={sessionChangesLoading}
            onOpenChanges={onOpenChanges}
          />
        ) : (
          <div className="chat-stream-blank" aria-hidden="true" />
        )
      ) : (
      <Virtuoso
        key={sessionId}
        ref={streamRef}
        scrollerRef={bindScroller}
        className="chat-stream chat-transcript chat-virtual-list"
        data={transcriptConversations}
        firstItemIndex={firstItemIndex}
        initialTopMostItemIndex={transcriptConversations.length - 1}
        computeItemKey={(_, conversation) => conversationTurnKey(conversation)}
        startReached={handleStartReached}
        endReached={handleEndReached}
        followOutput={followOutput}
        atBottomStateChange={handleAtBottomChange}
        rangeChanged={hydrateVisibleTurns}
        components={{
          Header: () => (
            <ChatHistoryPageHeader
              projectionState={projectionState}
              hasOlderMessages={hasOlderMessages}
              loadingOlderMessages={loadingOlderMessages}
              error={historyError ?? detailHydrationError}
              onRetry={retryHistory}
            />
          ),
          Footer: () => pendingActionError ? (
            <div className="chat-turn-action-error" role="alert">
              {pendingActionError}
            </div>
          ) : null,
        }}
        itemContent={(index, conversation) => (
            <div
              className="chat-virtual-turn"
              data-turn-id={conversationTurnKey(conversation)}
            >
              <div>
                <ChatTurnErrorBoundary
                  conversationId={conversation.conversationId}
                >
                  <ChatTurn
                    apiPort={apiPort}
                    workspaceId={workspaceId}
                    conversation={conversation}
                    showRawDetails={expandDetails}
                    isLastTurn={index === firstItemIndex + transcriptConversations.length - 1}
                    sessionBusy={sessionBusy}
                    onLoadAgentStateMessageRawContent={onLoadAgentStateMessageRawContent}
                    onLoadTurnDetails={onLoadTurnDetails}
                    onLoadToolDetails={onLoadToolDetails}
                    onReplayTurn={onReplayTurn}
                    onUpdatePending={updatePending}
                    onRemovePending={removePending}
                    onChangePendingPolicy={updatePendingPolicy}
                  />
                </ChatTurnErrorBoundary>
              </div>
            </div>
        )}
      />
      )}
      </section>
      {showJumpToLatest ? (
        <button
          type="button"
          className="chat-jump-to-latest"
          onClick={() => scrollToLatest("smooth")}
        >
          <span className="codicon codicon-arrow-down" aria-hidden="true" />
          跳到最新消息
        </button>
      ) : null}
    </section>
  );
}

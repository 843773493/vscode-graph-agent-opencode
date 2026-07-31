import React from "react";
import { Virtuoso } from "react-virtuoso";
import type {
  AttachmentRef,
  MessageReplayRequest,
  PendingRequestKind,
  PendingRequestOrderItem,
  SessionChangesSummary,
} from "../types/backend";
import type { ConversationView } from "../types/frontend";
import { conversationTurnKey } from "../state/session/turnDetailHydration";
import type { TurnProjectionState } from "../state/session/turnTimeline";
import ChatHistoryEmptyState from "./chat/ChatHistoryEmptyState";
import ChatHistoryPageHeader from "./chat/ChatHistoryPageHeader";
import ChatTurn from "./chat/ChatTurn";
import ChatTurnErrorBoundary from "./chat/ChatTurnErrorBoundary";
import { useTurnVirtualScroller } from "./chat/useTurnVirtualScroller";
import { useVisibleTurnDetailHydration } from "./chat/useVisibleTurnDetailHydration";

export default function ChatPanel({
  apiPort,
  workspaceId,
  conversations,
  expandDetails,
  hasActiveSession,
  hasOlderMessages,
  loadingOlderMessages,
  historyLoading,
  projectionState,
  timelineGeneration,
  projectionEpoch,
  historyError,
  onLoadOlderMessages,
  loadingDetailTurnIds,
  onLoadTurnDetails,
  onRetryHistory,
  sessionChangeSummary,
  sessionChangesLoading,
  onOpenChanges,
  onReplayTurn,
  onUpdatePending,
  onRemovePending,
  onClearPending,
  onReorderPending,
  onSendPendingImmediately,
}: {
  apiPort: number;
  workspaceId?: string | null;
  conversations: ConversationView[];
  expandDetails: boolean;
  hasActiveSession: boolean;
  hasOlderMessages: boolean;
  loadingOlderMessages: boolean;
  historyLoading: boolean;
  projectionState: TurnProjectionState;
  timelineGeneration: number;
  projectionEpoch: number | null;
  historyError: string | null;
  onLoadOlderMessages: () => Promise<void>;
  loadingDetailTurnIds: readonly string[];
  onLoadTurnDetails: (turnIds: string[]) => Promise<void>;
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
  onClearPending: () => Promise<void>;
  onReorderPending: (requests: PendingRequestOrderItem[]) => Promise<void>;
  onSendPendingImmediately: (messageId: string) => Promise<void>;
}): React.ReactNode {
  const [draggedPendingId, setDraggedPendingId] = React.useState<string | null>(null);
  const [pendingActionError, setPendingActionError] = React.useState<string | null>(null);
  const [pendingActionRunning, setPendingActionRunning] = React.useState(false);
  const sessionId = conversations[0]?.sessionId ?? "empty";
  const {
    bindScroller,
    firstItemIndex,
    followOutput,
    handleAtBottomChange,
    loadOlderPreservingAnchor,
    scrollToLatest,
    showJumpToLatest,
    streamRef,
  } = useTurnVirtualScroller({ conversations, sessionId, onLoadOlderMessages });
  const {
    detailHydrationError,
    clearDetailHydrationError,
    hydrateVisibleTurns,
  } = useVisibleTurnDetailHydration({
    sessionId,
    timelineGeneration,
    projectionEpoch,
    conversations,
    firstItemIndex,
    loadingTurnIds: loadingDetailTurnIds,
    onLoadTurnDetails,
  });
  const sessionBusy = conversations.some(
    (conversation) => conversation.status === "running" || conversation.status === "queued",
  );
  const pendingRequests = conversations
    .filter((conversation) =>
      conversation.pending
      && conversation.userMessage
      && conversation.pendingKind,
    )
    .map((conversation) => ({
      message_id: conversation.userMessage!.message_id,
      kind: conversation.pendingKind!,
    }));
  const firstSteeringId = pendingRequests.find(
    (request) => request.kind === "steering",
  )?.message_id ?? null;
  const firstQueuedId = pendingRequests.find(
    (request) => request.kind === "queued",
  )?.message_id ?? null;
  const firstPendingId = pendingRequests[0]?.message_id ?? null;

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

  const changePendingKind = React.useCallback(async (
    messageId: string,
    kind: PendingRequestKind,
  ) => {
    await onReorderPending(
      pendingRequests.map((request) =>
        request.message_id === messageId ? { ...request, kind } : request),
    );
  }, [onReorderPending, pendingRequests]);

  const dropPendingBefore = React.useCallback(async (targetMessageId: string) => {
    if (!draggedPendingId || draggedPendingId === targetMessageId) {
      return;
    }
    const reordered = [...pendingRequests];
    const sourceIndex = reordered.findIndex(
      (request) => request.message_id === draggedPendingId,
    );
    const targetIndex = reordered.findIndex(
      (request) => request.message_id === targetMessageId,
    );
    if (sourceIndex === -1 || targetIndex === -1) {
      throw new Error("拖拽重排时找不到待处理消息");
    }
    if (reordered[sourceIndex].kind !== reordered[targetIndex].kind) {
      setDraggedPendingId(null);
      return;
    }
    const [moved] = reordered.splice(sourceIndex, 1);
    reordered.splice(targetIndex, 0, moved);
    setDraggedPendingId(null);
    await onReorderPending(reordered);
  }, [draggedPendingId, onReorderPending, pendingRequests]);

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
  const sendPendingImmediately = React.useCallback((messageId: string) => runPendingAction(
    () => onSendPendingImmediately(messageId),
  ), [onSendPendingImmediately, runPendingAction]);
  const updatePendingKind = React.useCallback((
    messageId: string,
    kind: PendingRequestKind,
  ) => runPendingAction(
    () => changePendingKind(messageId, kind),
  ), [changePendingKind, runPendingAction]);
  const retryHistory = React.useCallback(() => {
    clearDetailHydrationError();
    onRetryHistory();
  }, [clearDetailHydrationError, onRetryHistory]);

  return (
    <section className="chat-stream-shell">
      <section
        data-expand-details={String(expandDetails)}
        className="chat-stream-virtual-shell"
      >
      {conversations.length === 0 ? (
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
        data={conversations}
        firstItemIndex={firstItemIndex}
        initialTopMostItemIndex={conversations.length - 1}
        computeItemKey={(_, conversation) => conversationTurnKey(conversation)}
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
              onLoadOlder={() => {
                void loadOlderPreservingAnchor().catch(() => undefined);
              }}
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
              {conversation.userMessage?.message_id === firstSteeringId ? (
                <div className="chat-pending-divider">
                  <span>引导消息 · {pendingRequests.filter((item) => item.kind === "steering").length}</span>
                  {conversation.userMessage?.message_id === firstPendingId ? (
                    <button
                      type="button"
                      disabled={pendingActionRunning}
                      onClick={() => void runPendingAction(onClearPending).catch(() => undefined)}
                    >
                      全部撤回
                    </button>
                  ) : null}
                </div>
              ) : null}
              {conversation.userMessage?.message_id === firstQueuedId ? (
                <div className="chat-pending-divider">
                  <span>排队消息 · {pendingRequests.filter((item) => item.kind === "queued").length}</span>
                  {conversation.userMessage?.message_id === firstPendingId ? (
                    <button
                      type="button"
                      disabled={pendingActionRunning}
                      onClick={() => void runPendingAction(onClearPending).catch(() => undefined)}
                    >
                      全部撤回
                    </button>
                  ) : null}
                </div>
              ) : null}
              <div
                className={conversation.pending ? "chat-pending-draggable" : undefined}
                onDragOver={(event) => {
                  const dragged = pendingRequests.find(
                    (request) => request.message_id === draggedPendingId,
                  );
                  if (
                    conversation.pending
                    && dragged
                    && dragged.kind === conversation.pendingKind
                  ) {
                    event.preventDefault();
                  }
                }}
                onDrop={(event) => {
                  if (!conversation.pending || !conversation.userMessage) {
                    return;
                  }
                  event.preventDefault();
                  void runPendingAction(
                    () => dropPendingBefore(conversation.userMessage!.message_id),
                  ).catch(() => undefined);
                }}
              >
                {conversation.pending && conversation.userMessage ? (
                  <button
                    type="button"
                    draggable={!pendingActionRunning}
                    className="chat-pending-drag-handle"
                    title="拖拽重排待处理消息"
                    aria-label="拖拽重排待处理消息"
                    onDragStart={() => {
                      setDraggedPendingId(conversation.userMessage!.message_id);
                    }}
                    onDragEnd={() => setDraggedPendingId(null)}
                  >
                    <span className="codicon codicon-gripper" aria-hidden="true" />
                  </button>
                ) : null}
                <ChatTurnErrorBoundary
                  conversationId={conversation.conversationId}
                >
                  <ChatTurn
                    apiPort={apiPort}
                    workspaceId={workspaceId}
                    conversation={conversation}
                    showRawDetails={expandDetails}
                    isLastTurn={index === firstItemIndex + conversations.length - 1}
                    sessionBusy={sessionBusy}
                    onReplayTurn={onReplayTurn}
                    onUpdatePending={updatePending}
                    onRemovePending={removePending}
                    onSendPendingImmediately={sendPendingImmediately}
                    onChangePendingKind={updatePendingKind}
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

import React from "react";
import { Virtuoso } from "react-virtuoso";
import type {
  Components as VirtuosoComponents,
  ComputeItemKey,
  ItemContent,
} from "react-virtuoso";
import type { TurnHistoryInclude } from "../../api/session/sessionTurnHistory";
import type {
  AttachmentRef,
  MessageReplayRequest,
  DeliveryPolicy,
  SessionChangesSummary,
  GatewayUserViewState,
} from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import { conversationTurnKey } from "../../state/session/turnIdentity";
import type { TurnProjectionState } from "../../state/session/turnTimeline";
import { isLiveConversationView } from "../../state/trace/traceAggregation";
import ChatHistoryEmptyState from "../chat/ChatHistoryEmptyState";
import ChatHistoryPageHeader from "../chat/ChatHistoryPageHeader";
import ChatTurn from "../chat/ChatTurn";
import ChatTurnErrorBoundary from "../chat/ChatTurnErrorBoundary";
import { useTurnVirtualScroller } from "../chat/useTurnVirtualScroller";
import { errorMessage } from "../../utils/errorMessage";

interface ChatPanelRenderState {
  apiPort: number;
  workspaceId?: string | null;
  expandDetails: boolean;
  firstItemIndex: number;
  transcriptLength: number;
  sessionBusy: boolean;
  onLoadAgentStateMessageRawContent: (
    sessionId: string,
    messageId: string,
  ) => Promise<string>;
  onLoadTurnDetails: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
    include?: TurnHistoryInclude[],
    toolCallIds?: string[],
  ) => Promise<void>;
  onLoadToolDetails?: (turnId: string, toolCallId: string) => Promise<void>;
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
  ) => Promise<void>;
  onOpenAttachment?: (sessionId: string, attachment: AttachmentRef) => void;
  projectionState: TurnProjectionState;
  hasOlderMessages: boolean;
  loadingOlderMessages: boolean;
  historyError: string | null;
  onRetryHistory: () => void;
  pendingActionError: string | null;
}

interface ChatPanelVirtuosoContext {
  stateRef: React.MutableRefObject<ChatPanelRenderState | null>;
}

function currentChatPanelRenderState(
  context: ChatPanelVirtuosoContext,
): ChatPanelRenderState {
  const state = context.stateRef.current;
  if (!state) {
    throw new Error("ChatPanel Virtuoso 渲染上下文尚未初始化");
  }
  return state;
}

function ChatPanelHistoryHeader({
  context,
}: {
  context: ChatPanelVirtuosoContext;
}): React.ReactNode {
  const state = currentChatPanelRenderState(context);
  const retryHistory = React.useCallback(() => {
    currentChatPanelRenderState(context).onRetryHistory();
  }, [context]);
  return (
    <ChatHistoryPageHeader
      projectionState={state.projectionState}
      hasOlderMessages={state.hasOlderMessages}
      loadingOlderMessages={state.loadingOlderMessages}
      error={state.historyError}
      onRetry={retryHistory}
    />
  );
}

function ChatPanelFooter({
  context,
}: {
  context: ChatPanelVirtuosoContext;
}): React.ReactNode {
  const { pendingActionError } = currentChatPanelRenderState(context);
  return pendingActionError ? (
    <div className="chat-turn-action-error" role="alert">
      {pendingActionError}
    </div>
  ) : null;
}

const CHAT_PANEL_VIRTUOSO_COMPONENTS: VirtuosoComponents<
  ConversationView,
  ChatPanelVirtuosoContext
> = {
  Header: ChatPanelHistoryHeader,
  Footer: ChatPanelFooter,
};

const computeChatPanelItemKey: ComputeItemKey<
  ConversationView,
  ChatPanelVirtuosoContext
> = (_, conversation) => conversationTurnKey(conversation);

const renderChatPanelItem: ItemContent<
  ConversationView,
  ChatPanelVirtuosoContext
> = (index, conversation, context) => {
  const state = currentChatPanelRenderState(context);
  return (
    <div
      className="chat-virtual-turn"
      data-turn-id={conversationTurnKey(conversation)}
    >
      <div>
        <ChatTurnErrorBoundary
          conversationId={conversation.conversationId}
        >
          <ChatTurn
            apiPort={state.apiPort}
            workspaceId={state.workspaceId}
            conversation={conversation}
            showRawDetails={state.expandDetails}
            isLastTurn={index === state.firstItemIndex + state.transcriptLength - 1}
            sessionBusy={state.sessionBusy}
            onLoadAgentStateMessageRawContent={state.onLoadAgentStateMessageRawContent}
            onLoadTurnDetails={state.onLoadTurnDetails}
            onLoadToolDetails={state.onLoadToolDetails}
            onReplayTurn={state.onReplayTurn}
            onUpdatePending={state.onUpdatePending}
            onRemovePending={state.onRemovePending}
            onChangePendingPolicy={state.onChangePendingPolicy}
            onOpenAttachment={state.onOpenAttachment}
          />
        </ChatTurnErrorBoundary>
      </div>
    </div>
  );
};

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
  historyError,
  onLoadAroundTurn,
  onLoadNewerMessages,
  onLoadOlderMessages,
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
  onOpenAttachment,
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
  historyError: string | null;
  onLoadAroundTurn: (anchorTurnId: string) => Promise<void>;
  onLoadNewerMessages: () => Promise<void>;
  onLoadOlderMessages: () => Promise<void>;
  onLoadTurnDetails: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
    include?: TurnHistoryInclude[],
    toolCallIds?: string[],
  ) => Promise<void>;
  onLoadToolDetails?: (turnId: string, toolCallId: string) => Promise<void>;
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
  onOpenAttachment?: (sessionId: string, attachment: AttachmentRef) => void;
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
      setPendingActionError(errorMessage(error));
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
    onRetryHistory();
  }, [onRetryHistory]);
  const renderStateRef = React.useRef<ChatPanelRenderState | null>(null);
  const virtuosoContext = React.useMemo<ChatPanelVirtuosoContext>(
    () => ({ stateRef: renderStateRef }),
    [
      expandDetails,
      firstItemIndex,
      transcriptConversations.length,
      sessionBusy,
      projectionState,
      hasOlderMessages,
      loadingOlderMessages,
      historyError,
      pendingActionError,
    ],
  );
  renderStateRef.current = {
    apiPort,
    workspaceId,
    expandDetails,
    firstItemIndex,
    transcriptLength: transcriptConversations.length,
    sessionBusy,
    onLoadAgentStateMessageRawContent,
    onLoadTurnDetails,
    onLoadToolDetails,
    onReplayTurn,
    onUpdatePending: updatePending,
    onRemovePending: removePending,
    onChangePendingPolicy: updatePendingPolicy,
    onOpenAttachment,
    projectionState,
    hasOlderMessages,
    loadingOlderMessages,
    historyError,
    onRetryHistory: retryHistory,
    pendingActionError,
  };

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
        context={virtuosoContext}
        data={transcriptConversations}
        firstItemIndex={firstItemIndex}
        initialTopMostItemIndex={transcriptConversations.length - 1}
        computeItemKey={computeChatPanelItemKey}
        startReached={handleStartReached}
        endReached={handleEndReached}
        followOutput={followOutput}
        atBottomStateChange={handleAtBottomChange}
        components={CHAT_PANEL_VIRTUOSO_COMPONENTS}
        itemContent={renderChatPanelItem}
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

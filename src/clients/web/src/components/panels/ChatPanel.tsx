import React from "react";
import { Virtuoso } from "react-virtuoso";
import type {
  Components as VirtuosoComponents,
  ComputeItemKey,
  ItemContent,
} from "react-virtuoso";
import type {
  AttachmentRef,
  DeliveryPolicy,
  SessionChangesSummary,
  GatewayUserViewState,
  InternalExecutionHistory,
  InternalDisplayProvenance,
} from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import { conversationTurnKey } from "../../state/session/turnIdentity";
import type { TurnProjectionState } from "../../state/session/turnTimeline";
import { isLiveConversationView } from "../../state/trace/traceAggregation";
import ChatHistoryEmptyState from "../chat/ChatHistoryEmptyState";
import ChatHistoryPageHeader from "../chat/ChatHistoryPageHeader";
import ChatTurn from "../chat/turn/ChatTurn";
import InternalExecutionTranscriptItem from "../chat/InternalExecutionTranscriptItem";
import ChatTurnErrorBoundary from "../chat/turn/ChatTurnErrorBoundary";
import { chatTranscriptItems, type ChatTranscriptItem } from "../chat/transcriptItems";
import { useTurnVirtualScroller } from "../chat/useTurnVirtualScroller";
import { errorMessage } from "../../utils/errorMessage";
import type { ChatTurnHandlers, LoadToolDetails, LoadTurnDetails } from "../chat/turn/types";

interface ChatPanelRenderState extends ChatTurnHandlers {
  apiPort: number;
  workspaceId?: string | null;
  sessionId: string | null;
  threadId: string | null;
  onOpenChildReport?: (source: InternalDisplayProvenance) => void;
  expandDetails: boolean;
  lastTurnKey: string | null;
  sessionBusy: boolean;
  onLoadAgentStateMessageRawContent: (
    sessionId: string,
    messageId: string,
  ) => Promise<string>;
  onLoadTurnDetails: LoadTurnDetails;
  onLoadToolDetails?: LoadToolDetails;
  onOpenAttachment?: (sessionId: string, attachment: AttachmentRef) => void;
  projectionState: TurnProjectionState;
  hasOlderMessages: boolean;
  loadingOlderMessages: boolean;
  historyError: string | null;
  onRetryHistory: () => void;
  pendingActionError: string | null;
  locatedTurnId: string | null;
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
  ChatTranscriptItem,
  ChatPanelVirtuosoContext
> = {
  Header: ChatPanelHistoryHeader,
  Footer: ChatPanelFooter,
};

const computeChatPanelItemKey: ComputeItemKey<
  ChatTranscriptItem,
  ChatPanelVirtuosoContext
> = (_, item) => item.key;

const renderChatPanelItem: ItemContent<
  ChatTranscriptItem,
  ChatPanelVirtuosoContext
> = (_index, item, context) => {
  const state = currentChatPanelRenderState(context);
  if (item.kind === "internal_execution") {
    const source = item.displayPart.provenance;
    const canOpenReport = state.onOpenChildReport && source.source_session_id === state.sessionId
      && source.source_thread_id && source.source_thread_id !== state.threadId && source.source_execution_id;
    return (
      <div className="chat-virtual-item">
        <InternalExecutionTranscriptItem displayPart={item.displayPart} />
        {canOpenReport ? <button type="button" onClick={() => state.onOpenChildReport?.(source)}>查看来源报告</button> : null}
      </div>
    );
  }
  return (
    <div
      className="chat-virtual-item"
      data-turn-id={item.conversation.turnId}
      aria-current={state.locatedTurnId === item.conversation.turnId ? "location" : undefined}
    >
      <div>
        <ChatTurnErrorBoundary
          conversationId={item.conversation.conversationId}
        >
          <ChatTurn
            apiPort={state.apiPort}
            workspaceId={state.workspaceId}
            conversation={item.conversation}
            showRawDetails={state.expandDetails}
            isLastTurn={item.key === state.lastTurnKey}
            sessionBusy={state.sessionBusy}
            onLoadAgentStateMessageRawContent={state.onLoadAgentStateMessageRawContent}
            onLoadTurnDetails={state.onLoadTurnDetails}
            onLoadToolDetails={state.onLoadToolDetails}
            onReplayTurn={state.onReplayTurn}
            onRemovePending={state.onRemovePending}
            onChangePendingPolicy={state.onChangePendingPolicy}
            onOpenAttachment={state.onOpenAttachment}
            onForkConversation={state.onForkConversation}
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

function TurnNavigationControls({ conversations, disabled, navigation, onNavigate }: {
  conversations: readonly ConversationView[];
  disabled: boolean;
  navigation: ReturnType<typeof useTurnVirtualScroller>["navigation"];
  onNavigate: (turnId: string) => void;
}): React.ReactNode {
  const [turnId, setTurnId] = React.useState("");
  const listId = React.useId();
  return (
    <form className="chat-turn-navigation" aria-label="跳到指定轮次" onSubmit={(event) => {
      event.preventDefault();
      onNavigate(turnId);
    }}>
      <label htmlFor={listId + "-input"}>轮次 ID</label>
      <input id={listId + "-input"} aria-label="轮次 ID" list={listId} value={turnId}
        placeholder="输入轮次 ID，或选择已加载轮次" disabled={disabled}
        onChange={(event) => setTurnId(event.target.value)} />
      <datalist id={listId}>
        {conversations.filter((turn) => turn.displayMode === "history" && !turn.pending && turn.turnId).map((turn) => (
          <option key={turn.turnId} value={turn.turnId}>
            {"第 " + turn.turnOrdinal + " 轮 · " + (turn.userMessage?.content.trim().replace(/\s+/g, " ").slice(0, 60) || "对话记录")}
          </option>
        ))}
      </datalist>
      <button type="submit" disabled={disabled || !turnId.trim()}>跳到指定轮次</button>
      {navigation ? <span role={navigation.phase === "error" ? "alert" : "status"}>{navigation.message}</span> : null}
    </form>
  );
}

export default function ChatPanel({
  apiPort,
  workspaceId,
  sessionId,
  threadId,
  conversations,
  internalExecutions = [],
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
  onRemovePending,
  onChangePendingPolicy,
  onOpenAttachment,
  onForkConversation,
  onOpenChildReport,
  viewState,
  onViewStateChange,
  onViewStateRestoreStatus,
}: {
  apiPort: number;
  workspaceId?: string | null;
  sessionId: string | null;
  threadId: string | null;
  conversations: ConversationView[];
  internalExecutions?: InternalExecutionHistory[];
  expandDetails: boolean;
  hasActiveSession: boolean;
  hasNewerMessages: boolean;
  hasOlderMessages: boolean;
  loadingNewerMessages: boolean;
  loadingOlderMessages: boolean;
  historyLoading: boolean;
  projectionState: TurnProjectionState;
  historyError: string | null;
  onLoadAroundTurn: (anchorTurnId: string, signal?: AbortSignal) => Promise<void>;
  onLoadNewerMessages: () => Promise<void>;
  onLoadOlderMessages: () => Promise<void>;
  onLoadTurnDetails: LoadTurnDetails;
  onLoadToolDetails?: LoadToolDetails;
  onLoadAgentStateMessageRawContent: (
    sessionId: string,
    messageId: string,
  ) => Promise<string>;
  onRetryHistory: () => void;
  sessionChangeSummary?: SessionChangesSummary | null;
  sessionChangesLoading?: boolean;
  onOpenChanges?: () => void;
  onOpenAttachment?: (sessionId: string, attachment: AttachmentRef) => void;
  onOpenChildReport?: (source: InternalDisplayProvenance) => void;
  viewState?: GatewayUserViewState | null;
  onViewStateChange?: (payload: {
    turn_anchor: string | null;
    scroll_offset: number;
    follow_latest: boolean;
  }) => void;
  onViewStateRestoreStatus?: (message: string) => void;
} & ChatTurnHandlers): React.ReactNode {
  const [pendingActionError, setPendingActionError] = React.useState<string | null>(null);
  const [pendingActionRunning, setPendingActionRunning] = React.useState(false);
  const transcriptConversations = React.useMemo(
    () => transcriptConversationsForDisplay(conversations),
    [conversations],
  );
  const transcriptItems = React.useMemo(
    () => chatTranscriptItems(transcriptConversations, internalExecutions),
    [transcriptConversations, internalExecutions],
  );
  const lastTurnKey = [...transcriptItems].reverse().find((item) => item.kind === "turn")?.key ?? null;
  const scopeKey = JSON.stringify([workspaceId ?? null, sessionId, threadId]);
  const {
    bindScroller,
    firstItemIndex,
    followOutput,
    handleAtBottomChange,
    handleEndReached,
    handleStartReached,
    navigateToTurn,
    navigation,
    scheduleAnchorAlignment,
    scrollToLatest,
    showJumpToLatest,
    streamRef,
  } = useTurnVirtualScroller({
    items: transcriptItems,
    scopeKey,
    onLoadNewerMessages,
    onLoadOlderMessages,
    onLoadAroundTurn,
    hasNewerMessages,
    hasOlderMessages,
    loadingNewerMessages,
    loadingOlderMessages,
    historyLoading,
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
      transcriptItems.length,
      lastTurnKey,
      sessionBusy,
      projectionState,
      hasOlderMessages,
      loadingOlderMessages,
      historyError,
      pendingActionError,
      navigation,
    ],
  );
  renderStateRef.current = {
    apiPort,
    workspaceId,
    sessionId,
    threadId,
    onOpenChildReport,
    expandDetails,
    lastTurnKey,
    sessionBusy,
    onLoadAgentStateMessageRawContent,
    onLoadTurnDetails,
    onLoadToolDetails,
    onReplayTurn,
    onRemovePending: removePending,
    onChangePendingPolicy: updatePendingPolicy,
    onOpenAttachment,
    onForkConversation,
    projectionState,
    hasOlderMessages,
    loadingOlderMessages,
    historyError,
    onRetryHistory: retryHistory,
    pendingActionError,
    locatedTurnId: navigation?.phase === "located" ? navigation.turnId : null,
  };

  return (
    <section className="chat-stream-shell">
      {hasActiveSession ? <TurnNavigationControls key={scopeKey} conversations={transcriptConversations}
        disabled={!sessionId || !threadId || historyLoading} navigation={navigation} onNavigate={navigateToTurn} /> : null}
      <section
        data-expand-details={String(expandDetails)}
        data-owner-session-id={sessionId ?? ""}
        data-owner-thread-id={threadId ?? ""}
        data-first-item-index={firstItemIndex}
        data-turn-count={transcriptConversations.length}
        data-transcript-item-count={transcriptItems.length}
        data-first-turn-id={transcriptConversations[0] ? conversationTurnKey(transcriptConversations[0]) : ""}
        className="chat-stream-virtual-shell"
      >
      {transcriptItems.length === 0 ? (
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
        key={scopeKey}
        ref={streamRef}
        scrollerRef={bindScroller}
        className="chat-stream chat-transcript chat-virtual-list"
        context={virtuosoContext}
        data={transcriptItems}
        firstItemIndex={firstItemIndex}
        initialTopMostItemIndex={transcriptItems.length - 1}
        computeItemKey={computeChatPanelItemKey}
        startReached={handleStartReached}
        endReached={handleEndReached}
        followOutput={followOutput}
        atBottomStateChange={handleAtBottomChange}
        itemsRendered={scheduleAnchorAlignment}
        totalListHeightChanged={scheduleAnchorAlignment}
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

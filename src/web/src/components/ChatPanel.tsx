import React from "react";
import type {
  AttachmentRef,
  MessageReplayRequest,
  PendingRequestKind,
  PendingRequestOrderItem,
  SessionChangesSummary,
} from "../types/backend";
import type { ConversationView } from "../types/frontend";
import ChatTurn from "./chat/ChatTurn";
import ChatTurnErrorBoundary from "./chat/ChatTurnErrorBoundary";

function conversationRenderKey(conversation: ConversationView): string {
  const lastEvent = conversation.events[conversation.events.length - 1];
  const assistantLength = (conversation.assistantMessages ?? []).reduce(
    (total, message) => total + message.content.length,
    0,
  );
  return [
    conversation.conversationId,
    conversation.status,
    conversation.events.length,
    lastEvent?.event_id ?? "",
    assistantLength,
  ].join(":");
}

export default function ChatPanel({
  apiPort,
  workspaceId,
  conversations,
  expandDetails,
  hasActiveSession,
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
  const streamRef = React.useRef<HTMLElement | null>(null);
  const followsLatestRef = React.useRef(true);
  const [showJumpToLatest, setShowJumpToLatest] = React.useState(false);
  const [draggedPendingId, setDraggedPendingId] = React.useState<string | null>(null);
  const [pendingActionError, setPendingActionError] = React.useState<string | null>(null);
  const [pendingActionRunning, setPendingActionRunning] = React.useState(false);
  const renderKey = conversations.map(conversationRenderKey).join("|");
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

  const scrollToLatest = React.useCallback((behavior: ScrollBehavior = "auto") => {
    const stream = streamRef.current;
    if (!stream) {
      return;
    }
    stream.scrollTo({ top: stream.scrollHeight, behavior });
    followsLatestRef.current = true;
    setShowJumpToLatest(false);
  }, []);

  React.useEffect(() => {
    if (followsLatestRef.current) {
      scrollToLatest();
    } else {
      setShowJumpToLatest(true);
    }
  }, [renderKey, scrollToLatest]);

  const handleScroll = React.useCallback(() => {
    const stream = streamRef.current;
    if (!stream) {
      return;
    }
    const distanceFromBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight;
    const followsLatest = distanceFromBottom <= 72;
    followsLatestRef.current = followsLatest;
    if (followsLatest) {
      setShowJumpToLatest(false);
    }
  }, []);

  return (
    <section className="chat-stream-shell">
      <section
        ref={streamRef}
        className="chat-stream chat-transcript"
        data-expand-details={String(expandDetails)}
        onScroll={handleScroll}
      >
        {conversations.length === 0 ? (
          hasActiveSession ? (
            <div className="chat-stream-empty-history" role="status">
              <div className="chat-stream-empty-title">该会话暂无历史消息</div>
              <div className="chat-stream-empty-detail">
                在下方输入任务，Assistant 的回复会显示在这里。
              </div>
              {sessionChangeSummary && sessionChangeSummary.files > 0 ? (
                <button
                  type="button"
                  className="chat-stream-empty-action"
                  onClick={onOpenChanges}
                >
                  本会话有 {sessionChangeSummary.files} 个文件变更待审查
                </button>
              ) : sessionChangesLoading ? (
                <div className="chat-stream-empty-detail">正在检查会话文件变更...</div>
              ) : null}
            </div>
          ) : (
            <div className="chat-stream-blank" aria-hidden="true" />
          )
        ) : (
          conversations.map((conversation, index) => (
            <React.Fragment key={conversation.conversationId}>
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
                    isLastTurn={index === conversations.length - 1}
                    sessionBusy={sessionBusy}
                    onReplayTurn={onReplayTurn}
                    onUpdatePending={(...args) => runPendingAction(
                      () => onUpdatePending(...args),
                    )}
                    onRemovePending={(messageId) => runPendingAction(
                      () => onRemovePending(messageId),
                    )}
                    onSendPendingImmediately={(messageId) => runPendingAction(
                      () => onSendPendingImmediately(messageId),
                    )}
                    onChangePendingKind={(messageId, kind) => runPendingAction(
                      () => changePendingKind(messageId, kind),
                    )}
                  />
                </ChatTurnErrorBoundary>
              </div>
            </React.Fragment>
          ))
        )}
        {pendingActionError ? (
          <div className="chat-turn-action-error" role="alert">
            {pendingActionError}
          </div>
        ) : null}
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

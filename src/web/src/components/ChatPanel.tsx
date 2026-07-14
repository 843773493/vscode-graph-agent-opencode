import React from "react";
import type { SessionChangesSummary } from "../types/backend";
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
  conversations,
  expandDetails,
  hasActiveSession,
  sessionChangeSummary,
  sessionChangesLoading,
  onOpenChanges,
}: {
  conversations: ConversationView[];
  expandDetails: boolean;
  hasActiveSession: boolean;
  sessionChangeSummary?: SessionChangesSummary | null;
  sessionChangesLoading?: boolean;
  onOpenChanges?: () => void;
}): React.ReactNode {
  const streamRef = React.useRef<HTMLElement | null>(null);
  const followsLatestRef = React.useRef(true);
  const [showJumpToLatest, setShowJumpToLatest] = React.useState(false);
  const renderKey = conversations.map(conversationRenderKey).join("|");

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
          conversations.map((conversation) => (
            <ChatTurnErrorBoundary
              key={conversation.conversationId}
              conversationId={conversation.conversationId}
            >
              <ChatTurn
                conversation={conversation}
                showRawDetails={expandDetails}
              />
            </ChatTurnErrorBoundary>
          ))
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

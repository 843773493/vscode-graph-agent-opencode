import React from "react";
import type { ConversationView } from "../../types/frontend";
import ChatTurnResponseBody from "./turn/ChatTurnResponseBody";
import {
  ChatTurnUserSection,
} from "./turn/ChatTurnUserActions";
import {
  useChatTurnActions,
  type ChatTurnActionCallbacks,
} from "./turn/useChatTurnActions";

export interface ChatTurnProps extends ChatTurnActionCallbacks {
  apiPort: number;
  workspaceId?: string | null;
  conversation: ConversationView;
  showRawDetails: boolean;
  isLastTurn: boolean;
  sessionBusy: boolean;
}

function ChatTurn({
  apiPort,
  workspaceId,
  conversation,
  showRawDetails,
  isLastTurn,
  sessionBusy,
  onReplayTurn,
  onUpdatePending,
  onRemovePending,
  onSendPendingImmediately,
  onChangePendingKind,
}: ChatTurnProps): React.ReactNode {
  const actions = useChatTurnActions({
    conversation,
    sessionBusy,
    onReplayTurn,
    onUpdatePending,
  });

  return (
    <article className="chat-turn" data-conversation-id={conversation.conversationId}>
      <ChatTurnUserSection
        apiPort={apiPort}
        workspaceId={workspaceId}
        conversation={conversation}
        sessionBusy={sessionBusy}
        actions={actions}
        onRemovePending={onRemovePending}
        onSendPendingImmediately={onSendPendingImmediately}
        onChangePendingKind={onChangePendingKind}
      />
      <div className="chat-assistant-row">
        <div className="chat-assistant-avatar" aria-hidden="true">
          <span className="codicon codicon-copilot" />
        </div>
        <div className="chat-assistant-content">
          <ChatTurnResponseBody
            conversation={conversation}
            showRawDetails={showRawDetails}
            isLastTurn={isLastTurn}
            sessionBusy={sessionBusy}
            actions={actions}
          />
        </div>
      </div>
    </article>
  );
}

function isActiveConversation(conversation: ConversationView): boolean {
  return conversation.pending
    || conversation.status === "running"
    || conversation.status === "queued";
}

function lastEventId(conversation: ConversationView): string | null {
  return conversation.events[conversation.events.length - 1]?.event_id ?? null;
}

function lastAssistantIdentity(conversation: ConversationView): string {
  const messages = conversation.assistantMessages ?? [];
  const last = messages[messages.length - 1];
  return last
    ? `${messages.length}:${last.message_id}:${last.updated_at}:${last.content.length}`
    : "0";
}

/**
 * 已完成 Turn 的 revision 是展示内容版本；活动/排队 Turn 则继续逐次渲染，
 * 防止尚未进入投影 revision 的 streaming/pending 更新被 memo 挡住。
 */
export function areChatTurnPropsEqual(
  previous: ChatTurnProps,
  next: ChatTurnProps,
): boolean {
  if (
    previous.apiPort !== next.apiPort
    || previous.workspaceId !== next.workspaceId
    || previous.showRawDetails !== next.showRawDetails
    || previous.isLastTurn !== next.isLastTurn
    || previous.sessionBusy !== next.sessionBusy
    || previous.onReplayTurn !== next.onReplayTurn
    || previous.onUpdatePending !== next.onUpdatePending
    || previous.onRemovePending !== next.onRemovePending
    || previous.onSendPendingImmediately !== next.onSendPendingImmediately
    || previous.onChangePendingKind !== next.onChangePendingKind
  ) {
    return false;
  }
  if (previous.conversation === next.conversation) {
    return true;
  }

  const left = previous.conversation;
  const right = next.conversation;
  if (
    !left.turnId
    || !right.turnId
    || isActiveConversation(left)
    || isActiveConversation(right)
  ) {
    return false;
  }
  return left.turnId === right.turnId
    && left.turnRevision === right.turnRevision
    && left.turnItemsView === right.turnItemsView
    && left.status === right.status
    && left.pending === right.pending
    && left.jobId === right.jobId
    && left.events.length === right.events.length
    && lastEventId(left) === lastEventId(right)
    && lastAssistantIdentity(left) === lastAssistantIdentity(right);
}

export default React.memo(ChatTurn, areChatTurnPropsEqual);

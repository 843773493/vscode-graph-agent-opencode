import React from "react";
import type { TurnHistoryInclude } from "../../api/sessionTurnHistory";
import { isLiveConversationView } from "../../state/trace/traceAggregation";
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
  onLoadAgentStateMessageRawContent: (
    sessionId: string,
    messageId: string,
  ) => Promise<string>;
  onLoadTurnDetails?: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
    include?: TurnHistoryInclude[],
    toolCallIds?: string[],
  ) => Promise<void>;
  onLoadToolDetails?: (turnId: string, toolCallId: string) => Promise<void>;
}

function ChatTurn({
  apiPort,
  workspaceId,
  conversation,
  showRawDetails,
  sessionBusy,
  onLoadAgentStateMessageRawContent,
  onLoadTurnDetails,
  onLoadToolDetails,
  onReplayTurn,
  onUpdatePending,
  onRemovePending,
  onChangePendingPolicy,
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
        onLoadAgentStateMessageRawContent={onLoadAgentStateMessageRawContent}
        onRemovePending={onRemovePending}
        onChangePendingPolicy={onChangePendingPolicy}
      />
      <div className="chat-assistant-row">
        <div className="chat-assistant-avatar-menu">
          <div className="chat-assistant-avatar" aria-hidden="true">
            <span className="codicon codicon-copilot" />
          </div>
        </div>
        <div className="chat-assistant-content">
          <ChatTurnResponseBody
            conversation={conversation}
            showRawDetails={showRawDetails}
            actions={actions}
            onLoadTurnDetails={onLoadTurnDetails}
            onLoadToolDetails={onLoadToolDetails}
          />
        </div>
      </div>
    </article>
  );
}

function isActiveConversation(conversation: ConversationView): boolean {
  return isLiveConversationView(conversation)
    && (conversation.pending
      || conversation.status === "running"
      || conversation.status === "queued");
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

function responsePartsEqual(
  left: ConversationView["responseParts"],
  right: ConversationView["responseParts"],
): boolean {
  if (left === right) return true;
  if (!left || !right || left.length !== right.length) return false;
  return left.every((part, index) => {
    const other = right[index];
    return part.part_id === other.part_id
      && part.kind === other.kind
      && part.projection === other.projection
      && part.status === other.status
      && part.text === other.text
      && part.carrier_type === other.carrier_type
      && part.tool_call_id === other.tool_call_id
      && part.tool_name === other.tool_name
      && part.arguments === other.arguments
      && part.result === other.result
      && part.truncated === other.truncated
      && part.final === other.final
      && part.outcome_unknown === other.outcome_unknown
      && part.completion_reason === other.completion_reason
      && part.partial === other.partial
      && part.source.message_sequence === other.source.message_sequence
      && part.source.content_block_index === other.source.content_block_index
      && part.source.item_index === other.source.item_index
      && part.source.call_index === other.source.call_index
      && part.source.result_message_sequence === other.source.result_message_sequence;
  });
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
    || previous.onLoadAgentStateMessageRawContent !== next.onLoadAgentStateMessageRawContent
    || previous.onLoadTurnDetails !== next.onLoadTurnDetails
    || previous.onLoadToolDetails !== next.onLoadToolDetails
    || previous.onReplayTurn !== next.onReplayTurn
    || previous.onUpdatePending !== next.onUpdatePending
    || previous.onRemovePending !== next.onRemovePending
    || previous.onChangePendingPolicy !== next.onChangePendingPolicy
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
    && responsePartsEqual(left.responseParts, right.responseParts)
    && left.events.length === right.events.length
    && lastEventId(left) === lastEventId(right)
    && lastAssistantIdentity(left) === lastAssistantIdentity(right);
}

export default React.memo(ChatTurn, areChatTurnPropsEqual);

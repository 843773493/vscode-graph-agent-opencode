import type { ProcessDetailSelection } from "../../../api/session/sessionTurnHistory";
import { ChatItemOwnerContext } from "../itemContextMenu";
import React from "react";
import ContextNotice from "../ContextNotice";
import { contextNoticeGroups } from "../../../state/timeline/responseItems";
import { MessageSearchLocation } from "../messageSearchLocation";
import type { TurnHistoryInclude } from "../../../api/session/sessionTurnHistory";
import type { AttachmentRef } from "../../../types/backend";
import { isLiveConversationView } from "../../../state/trace/traceAggregation";
import type { ConversationView } from "../../../types/frontend";
import ChatTurnResponseBody from "./ChatTurnResponseBody";
import {
  ChatTurnUserSection,
} from "./ChatTurnUserActions";
import {
  useChatTurnActions,
} from "./useChatTurnActions";
import type { ChatTurnHandlers } from "./types";

export interface ChatTurnProps extends ChatTurnHandlers {
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
    selection?: ProcessDetailSelection,
  ) => Promise<void>;
  onOpenAttachment?: (sessionId: string, attachment: AttachmentRef) => void;
}

function ChatTurn({
  apiPort,
  workspaceId,
  conversation,
  showRawDetails,
  sessionBusy,
  onLoadAgentStateMessageRawContent,
  onLoadTurnDetails,
  onReplayTurn,
  onRemovePending,
  onChangePendingPolicy,
  onOpenAttachment,
  onForkConversation,
}: ChatTurnProps): React.ReactNode {
  const actions = useChatTurnActions({
    conversation,
    sessionBusy,
    onReplayTurn,
  });
  return (
    <ChatItemOwnerContext.Provider value={conversation.threadId ? { sessionId: conversation.sessionId, threadId: conversation.threadId } : null}><article className="chat-turn" data-conversation-id={conversation.conversationId} data-turn-id={conversation.turnId} data-root-logical-item-ordinal={conversation.rootLogicalItemOrdinal}>
      <MessageSearchLocation turnId={conversation.turnId} />
      {contextNoticeGroups(conversation.responseItems ?? [], "before_user_message")
        .map(group => <ContextNotice key={group.id} notices={group.notices} />)}
      <ChatTurnUserSection
        apiPort={apiPort}
        workspaceId={workspaceId}
        conversation={conversation}
        sessionBusy={sessionBusy}
        actions={actions}
        onLoadAgentStateMessageRawContent={onLoadAgentStateMessageRawContent}
        onRemovePending={onRemovePending}
        onChangePendingPolicy={onChangePendingPolicy}
        onOpenAttachment={onOpenAttachment}
        onForkConversation={onForkConversation}
      />
      <div className="chat-assistant-row chat-assistant-content">
        <ChatTurnResponseBody
          conversation={conversation}
          workspaceId={workspaceId}
          showRawDetails={showRawDetails}
          actions={actions}
          onLoadTurnDetails={onLoadTurnDetails}
          onForkConversation={onForkConversation}
        />
      </div>
    </article></ChatItemOwnerContext.Provider>
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

function assistantMessagesMemoKey(conversation: ConversationView): string {
  const messages = conversation.assistantMessages ?? [];
  const last = messages[messages.length - 1];
  return last
    ? `${messages.length}:${last.message_id}:${last.updated_at}:${last.content.length}`
    : "0";
}

function responseItemsEqual(
  left: ConversationView["responseItems"],
  right: ConversationView["responseItems"],
): boolean {
  if (left === right) return true;
  if (!left || !right || left.length !== right.length) return false;
  return left.every((part, index) => {
    const other = right[index];
    return part.id === other.id
      && part.kind === other.kind
      && part.projection === other.projection
      && part.status === other.status
      && part.text === other.text
      && part.content === other.content
      && part.carrier_type === other.carrier_type
      && part.tool_call_id === other.tool_call_id
      && part.tool_name === other.tool_name
      && part.tool_execution === other.tool_execution
      && part.tool_outcome === other.tool_outcome
      && part.arguments === other.arguments
      && part.result === other.result
      && part.truncated === other.truncated
      && part.final === other.final
      && part.outcome_unknown === other.outcome_unknown
      && part.completion_reason === other.completion_reason
      && part.partial === other.partial
      && part.context_content_ref === other.context_content_ref
      && part.context_cursor === other.context_cursor
      && part.context_anchor === other.context_anchor
      && part.context_before_item_id === other.context_before_item_id
      && part.context_anchor_ordinal === other.context_anchor_ordinal
      && part.context_source_revision === other.context_source_revision
      && Object.keys(part.source).length === Object.keys(other.source).length
      && (Object.keys(part.source) as Array<keyof typeof part.source>).every(key => part.source[key] === other.source[key]);
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
    || previous.onReplayTurn !== next.onReplayTurn
    || previous.onRemovePending !== next.onRemovePending
    || previous.onChangePendingPolicy !== next.onChangePendingPolicy
    || previous.onOpenAttachment !== next.onOpenAttachment
    || previous.onForkConversation !== next.onForkConversation
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
    && left.turnStatus === right.turnStatus
    && left.turnFailure === right.turnFailure
    && left.sessionId === right.sessionId
    && left.threadId === right.threadId
    && left.sourceViewId === right.sourceViewId
    && left.sourceViewRevision === right.sourceViewRevision
    && left.activityOutline?.beforeCursor === right.activityOutline?.beforeCursor
    && left.historyTruncated === right.historyTruncated
    && left.bodyEvicted === right.bodyEvicted
    && left.turnItemsView === right.turnItemsView
    && left.status === right.status
    && left.pending === right.pending
    && left.jobId === right.jobId
    // Job 终态与消息流独立交付；同 Turn revision 仍须显示后到的失败与诊断。
    && left.messageStream?.connectionStatus === right.messageStream?.connectionStatus
    && left.messageStream?.streamStatus === right.messageStream?.streamStatus
    && left.messageStream?.lastEventSeq === right.messageStream?.lastEventSeq
    && left.messageStream?.failure === right.messageStream?.failure
    && left.messageStream?.protocolError === right.messageStream?.protocolError
    && left.messageStream?.activeState === right.messageStream?.activeState
    && left.messageStream?.activities === right.messageStream?.activities
    && left.messageStream?.resumable === right.messageStream?.resumable
    && responseItemsEqual(left.responseItems, right.responseItems)
    && left.events.length === right.events.length
    && lastEventId(left) === lastEventId(right)
    && assistantMessagesMemoKey(left) === assistantMessagesMemoKey(right);
}

export default React.memo(ChatTurn, areChatTurnPropsEqual);

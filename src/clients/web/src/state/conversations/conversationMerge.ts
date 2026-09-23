import type { ConversationView } from "../../types/frontend";
import { dedupeTraceEvents } from "../traceEvents";

/** Turn 已确认的终态集合；落入终态后，迟到的旧 SSE 不得再把它改回运行态。 */
export const TERMINAL_TURN_STATUSES = new Set([
  "completed",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
]);

function conversationStartTime(conversation: ConversationView): number {
  const messageTime = conversation.userMessage?.created_at;
  if (messageTime) {
    return new Date(messageTime).getTime();
  }
  const firstEvent = conversation.events[0];
  return firstEvent ? new Date(firstEvent.timestamp).getTime() : 0;
}

export function sortConversationViews(
  conversations: ConversationView[],
): ConversationView[] {
  return [...conversations].sort((left, right) => {
    if (left.pending !== right.pending) {
      return left.pending ? 1 : -1;
    }
    if (left.pending && right.pending) {
      return (
        (left.enqueueSequence ?? left.pendingPosition ?? Number.MAX_SAFE_INTEGER)
        - (right.enqueueSequence ?? right.pendingPosition ?? Number.MAX_SAFE_INTEGER)
      );
    }
    return conversationStartTime(left) - conversationStartTime(right);
  });
}

function conversationIdentityKey(conversation: ConversationView): string | null {
  const messageId = conversation.userMessage?.message_id ?? "";
  if (messageId) {
    return `message:${messageId}`;
  }

  const jobId = conversation.jobId ?? "";
  if (jobId) {
    return `job:${jobId}`;
  }

  return null;
}

export function conversationsMatch(
  left: ConversationView,
  right: ConversationView,
): boolean {
  const leftMessageId = left.userMessage?.message_id ?? "";
  const rightMessageId = right.userMessage?.message_id ?? "";
  if (leftMessageId && rightMessageId && leftMessageId === rightMessageId) {
    return true;
  }

  const leftJobId = left.jobId ?? "";
  const rightJobId = right.jobId ?? "";
  return Boolean(leftJobId && rightJobId && leftJobId === rightJobId);
}

export function mergeConversation(
  persisted: ConversationView,
  pending: ConversationView,
): ConversationView {
  const userMessage = persisted.userMessage && pending.userMessage
    ? {
        ...persisted.userMessage,
        ...pending.userMessage,
        // Turn 详情/摘要可能先于 live 状态到达；保留乐观 replay
        // 的操作元数据，否则回退提示会在新 Job 运行期间消失。
        metadata: {
          ...persisted.userMessage.metadata,
          ...pending.userMessage.metadata,
        },
      }
    : persisted.userMessage ?? pending.userMessage;
  const persistedTerminal = persisted.displayMode === "history"
    && Boolean(persisted.turnStatus)
    && TERMINAL_TURN_STATUSES.has(persisted.turnStatus!);
  if (persistedTerminal) {
    // terminal Turn 已经由后端 projection 确认后，完整替换 live 业务镜像；
    // pending 只贡献诊断事件和乐观操作元数据，不能重新暴露流式思考正文。
    return {
      ...pending,
      ...persisted,
      displayMode: "history",
      userMessage,
      events: dedupeTraceEvents([...persisted.events, ...pending.events]),
      pending: false,
      activeJobOverlay: false,
      source: "turn",
    };
  }
  const assistantMessages = [
    ...(persisted.assistantMessages ?? []),
    ...(pending.assistantMessages ?? []),
  ].filter(
    (message, index, all) =>
      all.findIndex((candidate) => candidate.message_id === message.message_id) === index,
  );
  return {
    ...persisted,
    ...pending,
    displayMode: pending.displayMode,
    userMessage,
    assistantMessages,
    events: dedupeTraceEvents([...persisted.events, ...pending.events]),
    source: pending.source === "pending" ? "pending" : persisted.source,
  };
}

export function dedupeConversationViews(
  conversations: ConversationView[],
): ConversationView[] {
  const merged: ConversationView[] = [];
  const seen = new Map<string, number>();

  for (const conversation of conversations) {
    const identityKey = conversationIdentityKey(conversation);
    if (!identityKey) {
      merged.push(conversation);
      continue;
    }

    const existingIndex = seen.get(identityKey);
    if (existingIndex === undefined) {
      seen.set(identityKey, merged.length);
      merged.push(conversation);
      continue;
    }

    merged[existingIndex] = mergeConversation(
      merged[existingIndex],
      conversation,
    );
  }

  return merged;
}

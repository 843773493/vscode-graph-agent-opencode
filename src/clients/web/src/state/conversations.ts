import type { AppState, ConversationView } from "../types/frontend";
import {
  conversationsMatch,
  dedupeConversationViews,
  mergeConversation,
  sortConversationViews,
} from "./conversations/conversationMerge";
import {
  applyMessageStreamProjection,
} from "./conversations/messageStreamProjection";
import { turnTimelineConversations } from "./conversations/turnProjection";

export { sortConversationViews } from "./conversations/conversationMerge";
export { messageStreamTurnIdForSession } from "./conversations/messageStreamProjection";
export { writePendingList } from "./conversations/pendingMap";
export {
  completePendingForJob,
  dispatchToConversationProjection,
  pendingSnapshotToConversations,
  preservePendingTerminalConversation,
  removePendingForTraceEvent,
  syncActiveJobConversation,
  writeDispatchActiveJob,
  writePendingSnapshot,
} from "./conversations/pendingQueue";
export {
  appendTraceEventsToPendingConversations,
  conversationMatchesTraceEvent,
  hasJobTerminalTraceEvent,
  PENDING_CONVERSATION_EVENT_LIMIT,
  statusForConversationEvents,
  traceEventsForConversation,
} from "./conversations/traceProjection";

export function getConversationsForSession(
  sessionId: string,
  state: AppState,
  sessionCacheKey: string = sessionId,
): ConversationView[] {
  const turnConversations = turnTimelineConversations(
    state,
    sessionCacheKey,
    sessionId,
  );
  const pendingList = state.pendingConversations.get(sessionCacheKey) ?? [];

  if (pendingList.length === 0) {
    return applyMessageStreamProjection(
      turnConversations,
      state.messageStreamsByTurnStream ?? new Map(),
    );
  }

  const merged = [...turnConversations];
  for (const pending of pendingList) {
    const matchedIndex = merged.findIndex((conversation) =>
      conversationsMatch(conversation, pending),
    );
    if (matchedIndex === -1) {
      merged.push({ ...pending, source: "pending" });
      continue;
    }

    merged[matchedIndex] = mergeConversation(merged[matchedIndex], pending);
  }

  return applyMessageStreamProjection(
    sortConversationViews(dedupeConversationViews(merged)),
    state.messageStreamsByTurnStream ?? new Map(),
  );
}

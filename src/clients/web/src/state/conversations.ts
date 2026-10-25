import type { AppState, ConversationView } from "../types/frontend";
import {
  conversationMatchKeys,
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
      sessionCacheKey,
    );
  }

  const merged = [...turnConversations];
  const indexesByMatchKey = new Map<string, Set<number>>();
  const matchKeysByIndex: string[][] = [];
  const addMatchIndex = (index: number, conversation: ConversationView): void => {
    const keys = conversationMatchKeys(conversation);
    matchKeysByIndex[index] = keys;
    for (const key of keys) {
      let indexes = indexesByMatchKey.get(key);
      if (!indexes) {
        indexes = new Set();
        indexesByMatchKey.set(key, indexes);
      }
      indexes.add(index);
    }
  };
  const removeMatchIndex = (index: number): void => {
    for (const key of matchKeysByIndex[index] ?? []) {
      const indexes = indexesByMatchKey.get(key);
      indexes?.delete(index);
      if (indexes?.size === 0) {
        indexesByMatchKey.delete(key);
      }
    }
  };
  for (let index = 0; index < merged.length; index += 1) {
    addMatchIndex(index, merged[index]!);
  }

  for (const pending of pendingList) {
    const pendingKeys = conversationMatchKeys(pending);
    let matchedIndex = -1;
    for (const key of pendingKeys) {
      for (const index of indexesByMatchKey.get(key) ?? []) {
        if ((matchedIndex === -1 || index < matchedIndex)
          && conversationsMatch(merged[index]!, pending)) {
          matchedIndex = index;
        }
      }
    }
    if (matchedIndex === -1) {
      const index = merged.length;
      merged.push({
        ...pending,
        // 当前 context view 没有该 Turn 时，不沿用旧视图的展示坐标。
        turnRevision: undefined,
        turnOrdinal: undefined,
        turnItemsView: undefined,
        rootLogicalItemOrdinal: undefined,
        source: "pending",
      });
      addMatchIndex(index, merged[index]!);
      continue;
    }

    removeMatchIndex(matchedIndex);
    merged[matchedIndex] = mergeConversation(merged[matchedIndex], pending);
    addMatchIndex(matchedIndex, merged[matchedIndex]!);
  }

  return applyMessageStreamProjection(
    sortConversationViews(dedupeConversationViews(merged)),
    state.messageStreamsByTurnStream ?? new Map(),
    sessionCacheKey,
  );
}

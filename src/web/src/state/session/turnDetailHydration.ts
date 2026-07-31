import type { ConversationView } from "../../types/frontend";

export const TURN_DETAIL_OVERSCAN = 1;
export const TURN_DETAIL_BATCH_LIMIT = 4;

export interface TurnVisibleRange {
  startIndex: number;
  endIndex: number;
}

export function conversationTurnKey(conversation: ConversationView): string {
  return conversation.turnId ?? conversation.conversationId;
}

function dataIndex(
  virtuosoIndex: number,
  firstItemIndex: number,
  itemCount: number,
): number {
  const offsetIndex = virtuosoIndex - firstItemIndex;
  if (offsetIndex >= 0 && offsetIndex < itemCount) {
    return offsetIndex;
  }
  if (virtuosoIndex >= 0 && virtuosoIndex < itemCount) {
    return virtuosoIndex;
  }
  return Math.min(Math.max(offsetIndex, 0), Math.max(0, itemCount - 1));
}

function hydrationTurnId(conversation: ConversationView): string | null {
  if (!conversation.turnId || conversation.turnItemsView !== "summary") {
    return null;
  }
  return conversation.turnId;
}

function chunkTurnIds(turnIds: string[]): string[][] {
  const batches: string[][] = [];
  for (let index = 0; index < turnIds.length; index += TURN_DETAIL_BATCH_LIMIT) {
    batches.push(turnIds.slice(index, index + TURN_DETAIL_BATCH_LIMIT));
  }
  return batches;
}

/**
 * Virtuoso 的 range 使用绝对索引；先选可视 Turn，再补固定一条上下 overscan。
 * 返回值已经去重并按服务端限制拆成不超过四条的批次。
 */
export function selectVisibleTurnDetailBatches({
  conversations,
  range,
  firstItemIndex,
  loadingTurnIds,
}: {
  conversations: ConversationView[];
  range: TurnVisibleRange;
  firstItemIndex: number;
  loadingTurnIds: readonly string[];
}): string[][] {
  if (conversations.length === 0) {
    return [];
  }

  const start = dataIndex(range.startIndex, firstItemIndex, conversations.length);
  const end = dataIndex(range.endIndex, firstItemIndex, conversations.length);
  const visibleStart = Math.min(start, end);
  const visibleEnd = Math.max(start, end);
  const candidateIndexes: number[] = [];
  for (let index = visibleStart; index <= visibleEnd; index += 1) {
    candidateIndexes.push(index);
  }
  for (let distance = 1; distance <= TURN_DETAIL_OVERSCAN; distance += 1) {
    candidateIndexes.push(visibleStart - distance, visibleEnd + distance);
  }

  const loading = new Set(loadingTurnIds);
  const selected = new Set<string>();
  for (const index of candidateIndexes) {
    if (index < 0 || index >= conversations.length) {
      continue;
    }
    const turnId = hydrationTurnId(conversations[index]);
    if (turnId && !loading.has(turnId)) {
      selected.add(turnId);
    }
  }
  return chunkTurnIds([...selected]);
}

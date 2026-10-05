export interface TranscriptVirtualIndexState {
  scopeKey: string;
  firstItemIndex: number;
  firstItemKey: string | null;
}

const TURN_VIRTUAL_INDEX_BASE = 1_000_000_000;

export function advanceTranscriptVirtualIndex(
  previous: TranscriptVirtualIndexState | null,
  scopeKey: string,
  orderedItemKeys: readonly string[],
): TranscriptVirtualIndexState {
  const firstItemKey = orderedItemKeys[0] ?? null;
  if (!previous || previous.scopeKey !== scopeKey || !previous.firstItemKey) {
    return {
      scopeKey,
      firstItemIndex: TURN_VIRTUAL_INDEX_BASE,
      firstItemKey,
    };
  }

  const previousFirstOffset = orderedItemKeys.indexOf(previous.firstItemKey);
  if (previousFirstOffset < 0) {
    return {
      scopeKey,
      firstItemIndex: TURN_VIRTUAL_INDEX_BASE,
      firstItemKey,
    };
  }
  if (previousFirstOffset > previous.firstItemIndex) {
    throw new Error(
      `Turn 虚拟列表前插数量越界: offset=${previousFirstOffset}, firstItemIndex=${previous.firstItemIndex}`,
    );
  }
  return {
    scopeKey,
    firstItemIndex: previous.firstItemIndex - previousFirstOffset,
    firstItemKey,
  };
}

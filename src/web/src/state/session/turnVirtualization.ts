export interface TurnVirtualIndexState {
  scopeKey: string;
  firstItemIndex: number;
  firstTurnId: string | null;
}

const TURN_VIRTUAL_INDEX_BASE = 1_000_000_000;

export function advanceTurnVirtualIndex(
  previous: TurnVirtualIndexState | null,
  scopeKey: string,
  orderedTurnIds: readonly string[],
): TurnVirtualIndexState {
  const firstTurnId = orderedTurnIds[0] ?? null;
  if (!previous || previous.scopeKey !== scopeKey || !previous.firstTurnId) {
    return {
      scopeKey,
      firstItemIndex: TURN_VIRTUAL_INDEX_BASE,
      firstTurnId,
    };
  }

  const previousFirstOffset = orderedTurnIds.indexOf(previous.firstTurnId);
  if (previousFirstOffset < 0) {
    return {
      scopeKey,
      firstItemIndex: TURN_VIRTUAL_INDEX_BASE,
      firstTurnId,
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
    firstTurnId,
  };
}

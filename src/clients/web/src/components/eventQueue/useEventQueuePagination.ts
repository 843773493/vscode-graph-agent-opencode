import React from "react";

import { useScrollWindow } from "./useScrollWindow";

const INITIAL_VISIBLE_EVENT_COUNT = 30;
const OLDER_EVENT_BATCH_SIZE = 30;

export function useEventQueuePagination({
  active,
  sessionId,
  displayItemCount,
  historyLoading,
  historyLoadingOlder,
  historyHasMore,
  onLoadOlderHistory,
}: {
  active: boolean;
  sessionId: string;
  displayItemCount: number;
  historyLoading: boolean;
  historyLoadingOlder: boolean;
  historyHasMore: boolean;
  onLoadOlderHistory: () => Promise<number>;
}) {
  const {
    appendVisibleCount,
    captureScrollAnchor,
    discardScrollAnchor,
    firstVisibleIndex,
    handleListScroll,
    listRef,
    revealOlderItems,
  } = useScrollWindow({
    active,
    sessionId,
    itemCount: displayItemCount,
    initialVisibleCount: INITIAL_VISIBLE_EVENT_COUNT,
    olderBatchSize: OLDER_EVENT_BATCH_SIZE,
    onExhaustedOlderItems: () => void loadOlderHistory(),
  });
  const serverLoadInFlightRef = React.useRef(false);

  const loadOlderHistory = React.useCallback(async () => {
    const list = listRef.current;
    if (
      !list
      || serverLoadInFlightRef.current
      || historyLoading
      || historyLoadingOlder
      || !historyHasMore
    ) return;
    serverLoadInFlightRef.current = true;
    captureScrollAnchor();
    try {
      const added = await onLoadOlderHistory();
      if (added > 0) appendVisibleCount(added);
      else discardScrollAnchor();
    } finally {
      serverLoadInFlightRef.current = false;
    }
  }, [
    appendVisibleCount,
    captureScrollAnchor,
    discardScrollAnchor,
    historyHasMore,
    historyLoading,
    historyLoadingOlder,
    listRef,
    onLoadOlderHistory,
  ]);

  const revealOlderEvents = React.useCallback(() => {
    if (revealOlderItems()) return;
    void loadOlderHistory();
  }, [loadOlderHistory, revealOlderItems]);

  return {
    firstVisibleIndex,
    handleListScroll,
    listRef,
    revealOlderEvents,
  };
}

import React from "react";

const LOAD_OLDER_SCROLL_THRESHOLD = 32;
const INITIAL_VISIBLE_EVENT_COUNT = 30;
const OLDER_EVENT_BATCH_SIZE = 30;
const useClientLayoutEffect = typeof window === "undefined"
  ? React.useEffect
  : React.useLayoutEffect;

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
  const listRef = React.useRef<HTMLDivElement | null>(null);
  const restoreScrollRef = React.useRef<{ height: number; top: number } | null>(null);
  const shouldScrollToLatestRef = React.useRef(true);
  const stickToLatestRef = React.useRef(true);
  const serverLoadInFlightRef = React.useRef(false);
  const [visibleCount, setVisibleCount] = React.useState(INITIAL_VISIBLE_EVENT_COUNT);
  const firstVisibleIndex = Math.max(displayItemCount - visibleCount, 0);

  useClientLayoutEffect(() => {
    if (!active) return;
    setVisibleCount(INITIAL_VISIBLE_EVENT_COUNT);
    shouldScrollToLatestRef.current = true;
    stickToLatestRef.current = true;
  }, [active, sessionId]);

  useClientLayoutEffect(() => {
    if (!active) return;
    const list = listRef.current;
    if (!list) return;
    const restore = restoreScrollRef.current;
    if (restore) {
      list.scrollTop = list.scrollHeight - restore.height + restore.top;
      restoreScrollRef.current = null;
      return;
    }
    if (shouldScrollToLatestRef.current || stickToLatestRef.current) {
      list.scrollTop = list.scrollHeight;
      shouldScrollToLatestRef.current = false;
    }
  }, [active, displayItemCount, sessionId, visibleCount]);

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
    restoreScrollRef.current = { height: list.scrollHeight, top: list.scrollTop };
    try {
      const added = await onLoadOlderHistory();
      if (added > 0) setVisibleCount((current) => current + added);
      else restoreScrollRef.current = null;
    } finally {
      serverLoadInFlightRef.current = false;
    }
  }, [historyHasMore, historyLoading, historyLoadingOlder, onLoadOlderHistory]);

  const revealOlderEvents = React.useCallback(() => {
    const list = listRef.current;
    if (!list) return;
    if (firstVisibleIndex > 0) {
      restoreScrollRef.current = { height: list.scrollHeight, top: list.scrollTop };
      setVisibleCount((current) =>
        Math.min(current + OLDER_EVENT_BATCH_SIZE, displayItemCount)
      );
      return;
    }
    void loadOlderHistory();
  }, [displayItemCount, firstVisibleIndex, loadOlderHistory]);

  const handleListScroll = React.useCallback(() => {
    const list = listRef.current;
    if (!list) return;
    stickToLatestRef.current =
      list.scrollHeight - list.scrollTop - list.clientHeight <= LOAD_OLDER_SCROLL_THRESHOLD;
    if (list.scrollTop <= LOAD_OLDER_SCROLL_THRESHOLD) revealOlderEvents();
  }, [revealOlderEvents]);

  return {
    firstVisibleIndex,
    handleListScroll,
    listRef,
    revealOlderEvents,
    visibleCount,
  };
}

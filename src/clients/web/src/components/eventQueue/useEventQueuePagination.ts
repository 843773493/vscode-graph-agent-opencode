import React from "react";

import { useScrollWindow, type ScrollWindowLoadTicket } from "./useScrollWindow";

const INITIAL_VISIBLE_EVENT_COUNT = 30;
const OLDER_EVENT_BATCH_SIZE = 30;

export function useEventQueuePagination({
  active,
  scopeKey,
  displayItemCount,
  historyLoading,
  historyLoadingOlder,
  historyHasMore,
  onLoadOlderHistory,
}: {
  active: boolean;
  scopeKey: string;
  displayItemCount: number;
  historyLoading: boolean;
  historyLoadingOlder: boolean;
  historyHasMore: boolean;
  onLoadOlderHistory: () => Promise<number>;
}) {
  const {
    beginOlderLoad,
    appendVisibleCount,
    captureScrollAnchor,
    discardScrollAnchor,
    firstVisibleIndex,
    handleListScroll,
    listRef,
    revealOlderItems,
  } = useScrollWindow({
    active,
    scopeKey,
    itemCount: displayItemCount,
    initialVisibleCount: INITIAL_VISIBLE_EVENT_COUNT,
    olderBatchSize: OLDER_EVENT_BATCH_SIZE,
    onExhaustedOlderItems: () => void loadOlderHistory(),
  });
  const serverLoadInFlightRef = React.useRef<ScrollWindowLoadTicket | null>(null);

  const loadOlderHistory = React.useCallback(async () => {
    const list = listRef.current;
    if (
      !list
      || historyLoading
      || historyLoadingOlder
      || !historyHasMore
    ) return;
    // 归属凭证在发起请求时确定：诊断作用域切换或窗口重置后返回的旧页必须被丢弃。
    const ticket = beginOlderLoad();
    const currentTicket = serverLoadInFlightRef.current;
    if (
      currentTicket
      && currentTicket.scopeKey === ticket.scopeKey
      && currentTicket.generation === ticket.generation
    ) return;
    serverLoadInFlightRef.current = ticket;
    captureScrollAnchor();
    try {
      const added = await onLoadOlderHistory();
      if (added > 0) appendVisibleCount(added, ticket);
      else discardScrollAnchor(ticket);
    } finally {
      if (serverLoadInFlightRef.current === ticket) {
        serverLoadInFlightRef.current = null;
      }
    }
  }, [
    beginOlderLoad,
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

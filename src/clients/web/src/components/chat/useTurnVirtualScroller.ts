import React from "react";
import type { VirtuosoHandle } from "react-virtuoso";
import {
  advanceTranscriptVirtualIndex,
  type TranscriptVirtualIndexState,
} from "../../state/session/turnVirtualization";
import type { ChatTranscriptItem } from "./transcriptItems";
import { errorMessage } from "../../utils/errorMessage";
import { useTurnAnchorNavigation, type TurnViewportPosition } from "./useTurnAnchorNavigation";

const SCROLL_POSITION_CHANGE_TOLERANCE_PX = 0.5;
const NATIVE_SCROLLBAR_HIT_WIDTH_PX = 24;

export function turnFollowOutput(
  followsLatest: boolean,
  isAtBottom: boolean,
): "auto" | false {
  return followsLatest && isAtBottom ? "auto" : false;
}

export function requestTurnFollowLatestFrame(
  requestFrame: (callback: FrameRequestCallback) => number,
  shouldFollow: () => boolean,
  scrollToLatest: () => void,
): number {
  return requestFrame(() => {
    if (shouldFollow()) {
      scrollToLatest();
    }
  });
}

function isNativeScrollbarPointer(
  event: MouseEvent | PointerEvent,
  scroller: HTMLElement,
): boolean {
  if (event.target !== scroller) return false;
  const primaryButtonDown = event.type === "mousemove" || event.type === "pointermove"
    ? event.buttons === 1
    : event.button === 0;
  if (!primaryButtonDown) return false;
  const bounds = scroller.getBoundingClientRect();
  return (
    event.clientX >= bounds.right - NATIVE_SCROLLBAR_HIT_WIDTH_PX
    && event.clientX <= bounds.right
    && event.clientY >= bounds.top
    && event.clientY <= bounds.bottom
  );
}

export function useTurnVirtualScroller({
  items,
  scopeKey,
  onLoadNewerMessages,
  onLoadOlderMessages,
  onLoadAroundTurn,
  hasNewerMessages,
  hasOlderMessages,
  loadingNewerMessages,
  loadingOlderMessages,
  historyLoading,
  viewState,
  onViewStateChange,
  onViewStateRestoreStatus,
}: {
  items: readonly ChatTranscriptItem[];
  scopeKey: string;
  onLoadNewerMessages: () => Promise<void>;
  onLoadOlderMessages: () => Promise<void>;
  onLoadAroundTurn: (anchorTurnId: string, signal?: AbortSignal) => Promise<void>;
  hasNewerMessages: boolean;
  hasOlderMessages: boolean;
  loadingNewerMessages: boolean;
  loadingOlderMessages: boolean;
  historyLoading: boolean;
  viewState?: TurnViewportPosition | null;
  onViewStateChange?: (payload: TurnViewportPosition) => void;
  onViewStateRestoreStatus?: (message: string) => void;
}) {
  const streamRef = React.useRef<VirtuosoHandle | null>(null);
  const scrollerRef = React.useRef<HTMLElement | null>(null);
  const followsLatestRef = React.useRef(true);
  const [showJumpToLatest, setShowJumpToLatest] = React.useState(false);
  const [nativeScrollbarDragging, setNativeScrollbarDragging] = React.useState(false);
  const virtualIndexRef = React.useRef<TranscriptVirtualIndexState | null>(null);
  const renderedFirstItemIndexRef = React.useRef<number | null>(null);
  const renderedFirstItemIndexScopeKeyRef = React.useRef<string | null>(null);
  const nativeScrollbarDragRef = React.useRef(false);
  const nativeScrollbarDragStartScrollTopRef = React.useRef(0);
  const nativeScrollbarDragMovedRef = React.useRef(false);
  const nativeScrollbarWasFollowingRef = React.useRef(false);
  const nativeScrollbarReleaseRef = React.useRef<{ scopeKey: string; scrollTop: number } | null>(null);
  const pointerButtonsRef = React.useRef(0);
  const finishNativeScrollbarDragRef = React.useRef<() => void>(() => undefined);
  const scrollEndCleanupRef = React.useRef<(() => void) | null>(null);
  const olderRequestActiveRef = React.useRef(false);
  const newerRequestActiveRef = React.useRef(false);
  const userRequestedOlderRef = React.useRef(false);
  const loadOlderMessagesRef = React.useRef<() => Promise<void>>(
    async () => undefined,
  );
  const viewStateTimerRef = React.useRef<number | null>(null);
  if (virtualIndexRef.current?.scopeKey !== scopeKey) {
    followsLatestRef.current = true;
    userRequestedOlderRef.current = false;
    nativeScrollbarReleaseRef.current = null;
  }
  const itemKeys = React.useMemo(() => items.map((item) => item.key), [items]);
  virtualIndexRef.current = advanceTranscriptVirtualIndex(
    virtualIndexRef.current,
    scopeKey,
    itemKeys,
  );
  const currentFirstItemIndex = virtualIndexRef.current.firstItemIndex;
  if (renderedFirstItemIndexScopeKeyRef.current !== scopeKey) {
    renderedFirstItemIndexScopeKeyRef.current = scopeKey;
    renderedFirstItemIndexRef.current = currentFirstItemIndex;
  }
  if (!nativeScrollbarDragging) {
    renderedFirstItemIndexRef.current = currentFirstItemIndex;
  }
  const firstItemIndex = nativeScrollbarDragging
    ? renderedFirstItemIndexRef.current ?? currentFirstItemIndex
    : currentFirstItemIndex;
  const layoutRevision = React.useMemo(() => items.map((item) => item.kind === "turn"
    ? [
      item.key,
      item.conversation.turnRevision ?? 0,
      item.conversation.turnItemsView ?? "pending",
      item.conversation.status,
    ].join(":")
    : `${item.key}:${item.displayPart.text}`).join("|"), [items]);

  const {
    anchorRef, cancelAnchor, navigateToTurn, navigation,
    scheduleAnchorAlignment, scrollToLatest,
  } = useTurnAnchorNavigation({
    items, scopeKey, streamRef, scrollerRef, followsLatestRef,
    setShowJumpToLatest, onLoadAroundTurn, historyLoading,
    viewState, onViewStateChange, onViewStateRestoreStatus,
  });
  const followOutput = React.useCallback(
    (isAtBottom: boolean) => turnFollowOutput(
      followsLatestRef.current,
      isAtBottom,
    ),
    [],
  );

  const bindScroller = React.useCallback((element: HTMLElement | Window | null) => {
    if (scrollerRef.current) {
      scrollerRef.current.onwheel = null;
      scrollerRef.current.ontouchstart = null;
      scrollerRef.current.onscroll = null;
    }
    scrollEndCleanupRef.current?.();
    scrollEndCleanupRef.current = null;
    const scroller = typeof HTMLElement !== "undefined"
      && element instanceof HTMLElement
      ? element
      : null;
    scrollerRef.current = scroller;
    if (!scroller) return;
    scheduleAnchorAlignment();
    const handleUserWheel = (event: WheelEvent) => {
      cancelAnchor();
      if (event.deltaY >= 0) return;
      userRequestedOlderRef.current = true;
      followsLatestRef.current = false;
      setShowJumpToLatest(true);
      if (scroller.scrollTop <= 2) {
        void loadOlderMessagesRef.current().catch((error: unknown) => {
          onViewStateRestoreStatus?.(
            `加载更早历史失败: ${errorMessage(error)}`,
          );
        });
      }
    };
    scroller.addEventListener("wheel", handleUserWheel, { passive: true });
    const handleScrollEnd = () => {
      if (nativeScrollbarDragRef.current) {
        if (pointerButtonsRef.current === 0) {
          finishNativeScrollbarDragRef.current();
        }
      }
    };
    scroller.addEventListener("scrollend", handleScrollEnd);
    scrollEndCleanupRef.current = () => {
      scroller.removeEventListener("wheel", handleUserWheel);
      scroller.removeEventListener("scrollend", handleScrollEnd);
    };
    scroller.onscroll = () => {
      if (
        nativeScrollbarDragRef.current
        && Math.abs(
          scroller.scrollTop - nativeScrollbarDragStartScrollTopRef.current,
        ) > SCROLL_POSITION_CHANGE_TOLERANCE_PX
      ) {
        nativeScrollbarDragMovedRef.current = true;
      }
      if (
        typeof window === "undefined"
        || !onViewStateChange
        || viewStateTimerRef.current !== null
      ) return;
      viewStateTimerRef.current = window.setTimeout(() => {
        viewStateTimerRef.current = null;
        if (virtualIndexRef.current?.scopeKey !== scopeKey || anchorRef.current) return;
        const scrollerTop = scroller.getBoundingClientRect().top;
        let anchor: HTMLElement | null = null;
        let anchorTop: number | null = null;
        for (const item of scroller.querySelectorAll<HTMLElement>("[data-turn-id]")) {
          const bounds = item.getBoundingClientRect();
          if (bounds.bottom <= scrollerTop) continue;
          anchor = item;
          anchorTop = bounds.top;
          break;
        }
        onViewStateChange({
          turn_anchor: anchor?.dataset.turnId ?? null,
          scroll_offset: anchorTop === null
            ? Math.max(0, scroller.scrollTop)
            : Math.max(0, scrollerTop - anchorTop),
          follow_latest: followsLatestRef.current,
        });
      }, 250);
    };
    scroller.ontouchstart = () => {
      cancelAnchor();
      followsLatestRef.current = false;
      setShowJumpToLatest(true);
    };
  }, [anchorRef, cancelAnchor, onViewStateChange, onViewStateRestoreStatus, scheduleAnchorAlignment, scopeKey]);

  React.useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const startNativeScrollbarDrag = (event: MouseEvent | PointerEvent) => {
      pointerButtonsRef.current = event.buttons;
      const scroller = scrollerRef.current;
      if (
        !scroller
        || !isNativeScrollbarPointer(event, scroller)
      ) {
        return;
      }
      userRequestedOlderRef.current = true;
      if (nativeScrollbarDragRef.current) return;
      nativeScrollbarWasFollowingRef.current = followsLatestRef.current;
      nativeScrollbarReleaseRef.current = null;
      cancelAnchor();
      followsLatestRef.current = false;
      setShowJumpToLatest(true);
      nativeScrollbarDragRef.current = true;
      nativeScrollbarDragStartScrollTopRef.current = scroller.scrollTop;
      nativeScrollbarDragMovedRef.current = false;
      setNativeScrollbarDragging(true);
    };
    const finishNativeScrollbarDrag = () => {
      if (!nativeScrollbarDragRef.current) return;
      pointerButtonsRef.current = 0;
      const userMovedScrollbar = nativeScrollbarDragMovedRef.current;
      if (userMovedScrollbar && scrollerRef.current) {
        if (virtualIndexRef.current && renderedFirstItemIndexRef.current !== virtualIndexRef.current.firstItemIndex) {
          nativeScrollbarReleaseRef.current = {
            scopeKey: virtualIndexRef.current.scopeKey,
            scrollTop: scrollerRef.current.scrollTop,
          };
        }
      } else if (nativeScrollbarWasFollowingRef.current) {
        followsLatestRef.current = true;
        setShowJumpToLatest(false);
      }
      nativeScrollbarDragRef.current = false;
      nativeScrollbarDragMovedRef.current = false;
      setNativeScrollbarDragging(false);
    };
    finishNativeScrollbarDragRef.current = finishNativeScrollbarDrag;

    window.addEventListener("pointerdown", startNativeScrollbarDrag, true);
    window.addEventListener("mousedown", startNativeScrollbarDrag, true);
    window.addEventListener("pointermove", startNativeScrollbarDrag, true);
    window.addEventListener("mousemove", startNativeScrollbarDrag, true);
    window.addEventListener("pointerup", finishNativeScrollbarDrag, true);
    window.addEventListener("mouseup", finishNativeScrollbarDrag, true);
    window.addEventListener("pointercancel", finishNativeScrollbarDrag, true);
    window.addEventListener("blur", finishNativeScrollbarDrag);
    return () => {
      window.removeEventListener("pointerdown", startNativeScrollbarDrag, true);
      window.removeEventListener("mousedown", startNativeScrollbarDrag, true);
      window.removeEventListener("pointermove", startNativeScrollbarDrag, true);
      window.removeEventListener("mousemove", startNativeScrollbarDrag, true);
      window.removeEventListener("pointerup", finishNativeScrollbarDrag, true);
      window.removeEventListener("mouseup", finishNativeScrollbarDrag, true);
      window.removeEventListener("pointercancel", finishNativeScrollbarDrag, true);
      window.removeEventListener("blur", finishNativeScrollbarDrag);
      scrollEndCleanupRef.current?.();
      scrollEndCleanupRef.current = null;
      finishNativeScrollbarDragRef.current = () => undefined;
    };
  }, [cancelAnchor]);

  React.useLayoutEffect(() => {
    if (
      typeof window === "undefined"
      || !followsLatestRef.current
      || items.length === 0
    ) return;
    const frame = requestTurnFollowLatestFrame(
      window.requestAnimationFrame.bind(window),
      () => followsLatestRef.current,
      () => scrollToLatest("auto", false),
    );
    return () => window.cancelAnimationFrame(frame);
  }, [items.length, layoutRevision, scrollToLatest]);

  React.useLayoutEffect(() => {
    if (nativeScrollbarDragging) return;
    const release = nativeScrollbarReleaseRef.current;
    nativeScrollbarReleaseRef.current = null;
    const scroller = scrollerRef.current;
    if (!release || !scroller || release.scopeKey !== scopeKey || typeof window === "undefined") return;
    // 只补偿本次释放提交时追平的前插索引；后继布局或新的用户输入会取消这一帧。
    const inputEvents = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
    let frame: number | null = null;
    const cleanup = () => {
      if (frame !== null) window.cancelAnimationFrame(frame);
      frame = null;
      for (const type of inputEvents) scroller.removeEventListener(type, cleanup, true);
    };
    for (const type of inputEvents) scroller.addEventListener(type, cleanup, { capture: true, passive: true });
    frame = window.requestAnimationFrame(() => {
      cleanup();
      if (
        scrollerRef.current !== scroller
        || virtualIndexRef.current?.scopeKey !== release.scopeKey
        || nativeScrollbarDragRef.current
        || followsLatestRef.current
        || anchorRef.current
      ) return;
      scroller.scrollTop = Math.min(release.scrollTop, Math.max(0, scroller.scrollHeight - scroller.clientHeight));
    });
    return cleanup;
  }, [anchorRef, firstItemIndex, layoutRevision, nativeScrollbarDragging, scopeKey]);

  React.useEffect(() => () => {
    if (viewStateTimerRef.current !== null) {
      if (typeof window !== "undefined") {
        window.clearTimeout(viewStateTimerRef.current);
      }
    }
  }, []);

  const loadOlderMessages = React.useCallback(async () => {
    if (
      olderRequestActiveRef.current
      || loadingOlderMessages
      || !hasOlderMessages
    ) return;
    olderRequestActiveRef.current = true;
    try {
      await onLoadOlderMessages();
    } finally {
      olderRequestActiveRef.current = false;
    }
  }, [hasOlderMessages, loadingOlderMessages, onLoadOlderMessages]);
  loadOlderMessagesRef.current = loadOlderMessages;

  const loadNewerMessages = React.useCallback(async () => {
    if (
      newerRequestActiveRef.current
      || loadingNewerMessages
      || !hasNewerMessages
    ) return;
    newerRequestActiveRef.current = true;
    try {
      await onLoadNewerMessages();
    } finally {
      newerRequestActiveRef.current = false;
    }
  }, [hasNewerMessages, loadingNewerMessages, onLoadNewerMessages]);

  const handleStartReached = React.useCallback(() => {
    if (!userRequestedOlderRef.current || anchorRef.current) return;
    void loadOlderMessages().catch((error: unknown) => {
      onViewStateRestoreStatus?.(
        `加载更早历史失败: ${errorMessage(error)}`,
      );
    });
  }, [anchorRef, loadOlderMessages, onViewStateRestoreStatus]);

  const handleEndReached = React.useCallback(() => {
    if (anchorRef.current) return;
    void loadNewerMessages().catch((error: unknown) => {
      onViewStateRestoreStatus?.(
        `加载更新历史失败: ${errorMessage(error)}`,
      );
    });
  }, [anchorRef, loadNewerMessages, onViewStateRestoreStatus]);

  const handleAtBottomChange = React.useCallback((atBottom: boolean) => {
    if (anchorRef.current || nativeScrollbarDragRef.current) return;
    if (atBottom) {
      followsLatestRef.current = true;
      setShowJumpToLatest(false);
    } else if (!followsLatestRef.current) {
      setShowJumpToLatest(true);
    }
  }, [anchorRef]);

  return {
    bindScroller,
    firstItemIndex,
    followOutput,
    handleAtBottomChange,
    handleEndReached,
    handleStartReached,
    navigateToTurn,
    navigation,
    scheduleAnchorAlignment,
    scrollToLatest,
    showJumpToLatest,
    streamRef,
  };
}

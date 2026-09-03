import React from "react";
import type { VirtuosoHandle } from "react-virtuoso";
import { conversationTurnKey } from "../../state/session/turnIdentity";
import {
  advanceTurnVirtualIndex,
  type TurnVirtualIndexState,
} from "../../state/session/turnVirtualization";
import type { ConversationView } from "../../types/frontend";

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

const MAX_VIEW_STATE_ANCHOR_LOAD_ATTEMPTS = 16;

export function useTurnVirtualScroller({
  conversations,
  sessionId,
  onLoadNewerMessages,
  onLoadOlderMessages,
  onLoadAroundTurn,
  hasNewerMessages,
  hasOlderMessages,
  loadingNewerMessages,
  loadingOlderMessages,
  viewState,
  onViewStateChange,
  onViewStateRestoreStatus,
}: {
  conversations: ConversationView[];
  sessionId: string;
  onLoadNewerMessages: () => Promise<void>;
  onLoadOlderMessages: () => Promise<void>;
  onLoadAroundTurn: (anchorTurnId: string) => Promise<void>;
  hasNewerMessages: boolean;
  hasOlderMessages: boolean;
  loadingNewerMessages: boolean;
  loadingOlderMessages: boolean;
  viewState?: {
    turn_anchor: string | null;
    scroll_offset: number;
    follow_latest: boolean;
  } | null;
  onViewStateChange?: (payload: {
    turn_anchor: string | null;
    scroll_offset: number;
    follow_latest: boolean;
  }) => void;
  onViewStateRestoreStatus?: (message: string) => void;
}) {
  const streamRef = React.useRef<VirtuosoHandle | null>(null);
  const scrollerRef = React.useRef<HTMLElement | null>(null);
  const followsLatestRef = React.useRef(true);
  const [showJumpToLatest, setShowJumpToLatest] = React.useState(false);
  const [nativeScrollbarDragging, setNativeScrollbarDragging] = React.useState(false);
  const virtualIndexRef = React.useRef<TurnVirtualIndexState | null>(null);
  const renderedFirstItemIndexRef = React.useRef<number | null>(null);
  const renderedFirstItemIndexSessionRef = React.useRef<string | null>(null);
  const nativeScrollbarDragRef = React.useRef(false);
  const nativeScrollbarDragStartScrollTopRef = React.useRef(0);
  const nativeScrollbarDragMovedRef = React.useRef(false);
  const deferredUserScrollTopRef = React.useRef<number | null>(null);
  const pointerButtonsRef = React.useRef(0);
  const finishNativeScrollbarDragRef = React.useRef<() => void>(() => undefined);
  const scrollEndCleanupRef = React.useRef<(() => void) | null>(null);
  const olderRequestActiveRef = React.useRef(false);
  const newerRequestActiveRef = React.useRef(false);
  const userRequestedOlderRef = React.useRef(false);
  const loadOlderMessagesRef = React.useRef<() => Promise<void>>(
    async () => undefined,
  );
  const viewStateRestoreRef = React.useRef<{
    sessionId: string;
    restored: boolean;
    loadingOlder: boolean;
    aroundRequested: boolean;
    attempts: number;
  } | null>(null);
  const viewStateTimerRef = React.useRef<number | null>(null);
  if (virtualIndexRef.current?.scopeKey !== sessionId) {
    followsLatestRef.current = true;
    userRequestedOlderRef.current = false;
  }
  virtualIndexRef.current = advanceTurnVirtualIndex(
    virtualIndexRef.current,
    sessionId,
    conversations.map(conversationTurnKey),
  );
  const currentFirstItemIndex = virtualIndexRef.current.firstItemIndex;
  if (renderedFirstItemIndexSessionRef.current !== sessionId) {
    renderedFirstItemIndexSessionRef.current = sessionId;
    renderedFirstItemIndexRef.current = currentFirstItemIndex;
  }
  if (!nativeScrollbarDragging) {
    renderedFirstItemIndexRef.current = currentFirstItemIndex;
  }
  const firstItemIndex = nativeScrollbarDragging
    ? renderedFirstItemIndexRef.current ?? currentFirstItemIndex
    : currentFirstItemIndex;
  const layoutRevision = conversations.map((conversation) => [
    conversationTurnKey(conversation),
    conversation.turnRevision ?? 0,
    conversation.turnItemsView ?? "pending",
    conversation.status,
  ].join(":")).join("|");

  const scrollToLatest = React.useCallback((behavior: "auto" | "smooth" = "auto") => {
    if (!streamRef.current || conversations.length === 0) return;
    streamRef.current.scrollToIndex({
      index: conversations.length - 1,
      align: "end",
      behavior,
    });
    followsLatestRef.current = true;
    setShowJumpToLatest(false);
  }, [conversations.length]);
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
    const handleUserWheel = (event: WheelEvent) => {
      if (event.deltaY >= 0) return;
      userRequestedOlderRef.current = true;
      followsLatestRef.current = false;
      setShowJumpToLatest(true);
      if (scroller.scrollTop <= 2) {
        void loadOlderMessagesRef.current().catch((error: unknown) => {
          onViewStateRestoreStatus?.(
            `加载更早历史失败: ${error instanceof Error ? error.message : String(error)}`,
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
        const scrollerTop = scroller.getBoundingClientRect().top;
        const anchor = Array.from(
          scroller.querySelectorAll<HTMLElement>("[data-turn-id]"),
        ).find((item) => item.getBoundingClientRect().bottom > scrollerTop) ?? null;
        onViewStateChange({
          turn_anchor: anchor?.dataset.turnId ?? null,
          scroll_offset: anchor
            ? Math.max(0, anchor.getBoundingClientRect().top - scrollerTop)
            : Math.max(0, scroller.scrollTop),
          follow_latest: followsLatestRef.current,
        });
      }, 250);
    };
    scroller.ontouchstart = () => {
      followsLatestRef.current = false;
      setShowJumpToLatest(true);
    };
  }, [onViewStateChange, onViewStateRestoreStatus]);

  React.useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    if (viewStateRestoreRef.current?.sessionId !== sessionId) {
      viewStateRestoreRef.current = {
        sessionId,
        restored: false,
        loadingOlder: false,
        aroundRequested: false,
        attempts: 0,
      };
      followsLatestRef.current = true;
    }
    const restoration = viewStateRestoreRef.current;
    if (!viewState || !restoration || restoration.restored) return;
    if (viewState.follow_latest) {
      restoration.restored = true;
      followsLatestRef.current = true;
      scrollToLatest("auto");
      return;
    }
    const anchorIndex = viewState.turn_anchor
      ? conversations.findIndex(
          (conversation) => conversationTurnKey(conversation) === viewState.turn_anchor,
        )
      : -1;
    if (anchorIndex < 0) {
      if (
        viewState.turn_anchor
        && !restoration.aroundRequested
        && !loadingOlderMessages
        && !loadingNewerMessages
      ) {
        restoration.aroundRequested = true;
        restoration.loadingOlder = true;
        restoration.attempts += 1;
        void onLoadAroundTurn(viewState.turn_anchor)
          .catch((error: unknown) => {
            const message = error instanceof Error ? error.message : String(error);
            onViewStateRestoreStatus?.(`恢复锚点历史失败: ${message}`);
          })
          .finally(() => {
            restoration.loadingOlder = false;
          });
        return;
      }
      if (
        viewState.turn_anchor
        && hasOlderMessages
        && !loadingOlderMessages
        && !restoration.loadingOlder
        && restoration.attempts < MAX_VIEW_STATE_ANCHOR_LOAD_ATTEMPTS
      ) {
        restoration.loadingOlder = true;
        restoration.attempts += 1;
        void onLoadOlderMessages()
          .catch((error: unknown) => {
            const message = error instanceof Error ? error.message : String(error);
            onViewStateRestoreStatus?.(`恢复历史位置失败: ${message}`);
          })
          .finally(() => {
            restoration.loadingOlder = false;
          });
        return;
      }
      if (
        !loadingOlderMessages
        && (!hasOlderMessages || restoration.attempts >= MAX_VIEW_STATE_ANCHOR_LOAD_ATTEMPTS)
      ) {
        restoration.restored = true;
        followsLatestRef.current = true;
        scrollToLatest("auto");
        onViewStateRestoreStatus?.(
          restoration.attempts >= MAX_VIEW_STATE_ANCHOR_LOAD_ATTEMPTS
            ? "历史位置未找到，已回到会话尾部"
            : "历史位置已失效，已回到会话尾部",
        );
      }
      return;
    }
    if (!streamRef.current || !scrollerRef.current) return;
    restoration.restored = true;
    followsLatestRef.current = false;
    streamRef.current.scrollToIndex({
      index: firstItemIndex + anchorIndex,
      align: "start",
      behavior: "auto",
    });
    window.requestAnimationFrame(() => {
      if (scrollerRef.current) {
        scrollerRef.current.scrollTop += viewState.scroll_offset;
      }
    });
  }, [
    conversations,
    firstItemIndex,
    hasOlderMessages,
    loadingNewerMessages,
    loadingOlderMessages,
    onLoadAroundTurn,
    onLoadOlderMessages,
    onViewStateRestoreStatus,
    scrollToLatest,
    sessionId,
    viewState,
  ]);

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
        deferredUserScrollTopRef.current = scrollerRef.current.scrollTop;
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
  }, []);

  React.useLayoutEffect(() => {
    if (
      typeof window === "undefined"
      || !followsLatestRef.current
      || conversations.length === 0
    ) return;
    const frame = requestTurnFollowLatestFrame(
      window.requestAnimationFrame.bind(window),
      () => followsLatestRef.current,
      () => scrollToLatest("auto"),
    );
    return () => window.cancelAnimationFrame(frame);
  }, [conversations.length, layoutRevision, scrollToLatest]);

  React.useLayoutEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const deferredUserScrollTop = deferredUserScrollTopRef.current;
    if (deferredUserScrollTop !== null) {
      deferredUserScrollTopRef.current = null;
      const scroller = scrollerRef.current;
      if (scroller) {
        window.requestAnimationFrame(() => {
          if (scrollerRef.current !== scroller) return;
          scroller.scrollTop = Math.min(
            deferredUserScrollTop,
            Math.max(0, scroller.scrollHeight - scroller.clientHeight),
          );
        });
      }
      return;
    }
  }, [firstItemIndex, layoutRevision]);

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
    if (!userRequestedOlderRef.current) return;
    void loadOlderMessages().catch((error: unknown) => {
      onViewStateRestoreStatus?.(
        `加载更早历史失败: ${error instanceof Error ? error.message : String(error)}`,
      );
    });
  }, [loadOlderMessages, onViewStateRestoreStatus]);

  const handleEndReached = React.useCallback(() => {
    void loadNewerMessages().catch((error: unknown) => {
      onViewStateRestoreStatus?.(
        `加载更新历史失败: ${error instanceof Error ? error.message : String(error)}`,
      );
    });
  }, [loadNewerMessages, onViewStateRestoreStatus]);

  const handleAtBottomChange = React.useCallback((atBottom: boolean) => {
    if (atBottom) {
      followsLatestRef.current = true;
      setShowJumpToLatest(false);
    } else if (!followsLatestRef.current) {
      setShowJumpToLatest(true);
    }
  }, []);

  return {
    bindScroller,
    firstItemIndex,
    followOutput,
    handleAtBottomChange,
    handleEndReached,
    handleStartReached,
    scrollToLatest,
    showJumpToLatest,
    streamRef,
  };
}

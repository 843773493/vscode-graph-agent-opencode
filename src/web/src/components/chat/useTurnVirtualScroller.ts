import React from "react";
import type { VirtuosoHandle } from "react-virtuoso";
import { conversationTurnKey } from "../../state/session/turnDetailHydration";
import {
  advanceTurnVirtualIndex,
  type TurnVirtualIndexState,
} from "../../state/session/turnVirtualization";
import type { ConversationView } from "../../types/frontend";

const TURN_ANCHOR_RESTORE_MIN_FRAMES = 30;
const TURN_ANCHOR_RESTORE_MAX_FRAMES = 60;
const TURN_ANCHOR_RESTORE_STABLE_FRAMES = 4;
const TURN_ANCHOR_RESTORE_TOLERANCE_PX = 0.5;

export function turnDataIndexFromAbsolute(
  absoluteIndex: number,
  firstItemIndex: number,
): number {
  const dataIndex = absoluteIndex - firstItemIndex;
  if (!Number.isSafeInteger(dataIndex) || dataIndex < 0) {
    throw new Error(
      `Turn 绝对索引无法映射到当前列表: absolute=${absoluteIndex}, first=${firstItemIndex}`,
    );
  }
  return dataIndex;
}

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

interface PendingTurnAnchor {
  sequence: number;
  scroller: HTMLElement;
  anchorId: string;
  anchorTop: number;
  absoluteIndex: number;
  indexScrollRequested: boolean;
}

function measureTurnAnchorDelta(anchor: PendingTurnAnchor): number | null {
  const restoredAnchor = Array.from(
    anchor.scroller.querySelectorAll<HTMLElement>("[data-turn-id]"),
  ).find((element) => element.dataset.turnId === anchor.anchorId);
  if (!restoredAnchor) return null;
  return restoredAnchor.getBoundingClientRect().top
    - anchor.scroller.getBoundingClientRect().top
    - anchor.anchorTop;
}

export async function restoreTurnAnchorPosition({
  measureDelta,
  applyDelta,
  nextFrame,
  isActive,
  minimumFrames = TURN_ANCHOR_RESTORE_MIN_FRAMES,
  maximumFrames = TURN_ANCHOR_RESTORE_MAX_FRAMES,
  stableFrames = TURN_ANCHOR_RESTORE_STABLE_FRAMES,
  tolerancePx = TURN_ANCHOR_RESTORE_TOLERANCE_PX,
}: {
  measureDelta: () => number | null;
  applyDelta: (delta: number) => void;
  nextFrame: () => Promise<void>;
  isActive: () => boolean;
  minimumFrames?: number;
  maximumFrames?: number;
  stableFrames?: number;
  tolerancePx?: number;
}): Promise<void> {
  let consecutiveStableFrames = 0;
  for (let frame = 0; frame < maximumFrames; frame += 1) {
    await nextFrame();
    if (!isActive()) return;
    const delta = measureDelta();
    if (delta === null) {
      consecutiveStableFrames = 0;
      continue;
    }
    if (!Number.isFinite(delta)) {
      throw new Error(`Turn 锚点偏移不是有限数值: ${delta}`);
    }
    if (Math.abs(delta) > tolerancePx) {
      applyDelta(delta);
      consecutiveStableFrames = 0;
    } else {
      consecutiveStableFrames += 1;
    }
    if (
      frame + 1 >= minimumFrames
      && consecutiveStableFrames >= stableFrames
    ) {
      return;
    }
  }
}

export function useTurnVirtualScroller({
  conversations,
  sessionId,
  onLoadOlderMessages,
}: {
  conversations: ConversationView[];
  sessionId: string;
  onLoadOlderMessages: () => Promise<void>;
}) {
  const streamRef = React.useRef<VirtuosoHandle | null>(null);
  const scrollerRef = React.useRef<HTMLElement | null>(null);
  const followsLatestRef = React.useRef(true);
  const [showJumpToLatest, setShowJumpToLatest] = React.useState(false);
  const virtualIndexRef = React.useRef<TurnVirtualIndexState | null>(null);
  const anchorRestorationSequenceRef = React.useRef(0);
  const pendingAnchorRef = React.useRef<PendingTurnAnchor | null>(null);
  if (virtualIndexRef.current?.scopeKey !== sessionId) {
    followsLatestRef.current = true;
  }
  virtualIndexRef.current = advanceTurnVirtualIndex(
    virtualIndexRef.current,
    sessionId,
    conversations.map(conversationTurnKey),
  );
  const firstItemIndex = virtualIndexRef.current.firstItemIndex;
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
    }
    const scroller = element instanceof HTMLElement ? element : null;
    scrollerRef.current = scroller;
    if (!scroller) return;
    scroller.onwheel = (event) => {
      if (event.deltaY < 0) {
        followsLatestRef.current = false;
        setShowJumpToLatest(true);
      }
    };
    scroller.ontouchstart = () => {
      followsLatestRef.current = false;
      setShowJumpToLatest(true);
    };
  }, []);

  React.useLayoutEffect(() => {
    if (!followsLatestRef.current || conversations.length === 0) return;
    const frame = requestTurnFollowLatestFrame(
      window.requestAnimationFrame.bind(window),
      () => followsLatestRef.current,
      () => scrollToLatest("auto"),
    );
    return () => window.cancelAnimationFrame(frame);
  }, [conversations.length, layoutRevision, scrollToLatest]);

  React.useLayoutEffect(() => {
    const pending = pendingAnchorRef.current;
    if (
      !pending
      || pending.sequence !== anchorRestorationSequenceRef.current
      || pending.scroller !== scrollerRef.current
    ) {
      return;
    }
    const delta = measureTurnAnchorDelta(pending);
    if (delta === null && !pending.indexScrollRequested) {
      pending.indexScrollRequested = true;
      const currentFirstItemIndex = virtualIndexRef.current?.firstItemIndex;
      if (currentFirstItemIndex === undefined) {
        throw new Error("Turn 锚点恢复缺少当前 firstItemIndex");
      }
      streamRef.current?.scrollToIndex({
        index: turnDataIndexFromAbsolute(
          pending.absoluteIndex,
          currentFirstItemIndex,
        ),
        align: "start",
        behavior: "auto",
      });
      return;
    }
    if (delta !== null && Math.abs(delta) > TURN_ANCHOR_RESTORE_TOLERANCE_PX) {
      pending.scroller.scrollTop += delta;
    }
  }, [firstItemIndex, layoutRevision]);

  React.useEffect(() => () => {
    anchorRestorationSequenceRef.current += 1;
  }, []);

  const loadOlderPreservingAnchor = React.useCallback(async () => {
    const scroller = scrollerRef.current;
    const scrollerTop = scroller?.getBoundingClientRect().top ?? 0;
    const anchor = scroller
      ? Array.from(scroller.querySelectorAll<HTMLElement>("[data-turn-id]"))
        .find((element) => element.getBoundingClientRect().bottom > scrollerTop) ?? null
      : null;
    const anchorId = anchor?.dataset.turnId ?? null;
    const anchorTop = anchor ? anchor.getBoundingClientRect().top - scrollerTop : null;
    const anchorDataIndex = anchorId
      ? conversations.findIndex(
          (conversation) => conversationTurnKey(conversation) === anchorId,
        )
      : -1;
    const restorationSequence = anchorRestorationSequenceRef.current + 1;
    anchorRestorationSequenceRef.current = restorationSequence;
    const pending = scroller && anchorId && anchorTop !== null && anchorDataIndex >= 0
      ? {
          sequence: restorationSequence,
          scroller,
          anchorId,
          anchorTop,
          absoluteIndex: firstItemIndex + anchorDataIndex,
          indexScrollRequested: false,
        }
      : null;
    pendingAnchorRef.current = pending;
    try {
      await onLoadOlderMessages();
      if (!pending) return;
      if (measureTurnAnchorDelta(pending) === null && !pending.indexScrollRequested) {
        pending.indexScrollRequested = true;
        const currentFirstItemIndex = virtualIndexRef.current?.firstItemIndex;
        if (currentFirstItemIndex === undefined) {
          throw new Error("Turn 锚点恢复缺少当前 firstItemIndex");
        }
        streamRef.current?.scrollToIndex({
          index: turnDataIndexFromAbsolute(
            pending.absoluteIndex,
            currentFirstItemIndex,
          ),
          align: "start",
          behavior: "auto",
        });
      }
      await restoreTurnAnchorPosition({
        nextFrame: () => new Promise<void>((resolve) => {
          window.requestAnimationFrame(() => resolve());
        }),
        isActive: () =>
          anchorRestorationSequenceRef.current === restorationSequence
          && scrollerRef.current === pending.scroller,
        measureDelta: () => measureTurnAnchorDelta(pending),
        applyDelta: (delta) => {
          pending.scroller.scrollTop += delta;
        },
      });
    } finally {
      if (pendingAnchorRef.current?.sequence === restorationSequence) {
        pendingAnchorRef.current = null;
      }
    }
  }, [conversations, firstItemIndex, onLoadOlderMessages]);

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
    loadOlderPreservingAnchor,
    scrollToLatest,
    showJumpToLatest,
    streamRef,
  };
}

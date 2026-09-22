import React from "react";

/** 判定「已贴近底部」的滚动阈值；请求视图与事件视图共用同一口径。 */
export const LOAD_OLDER_SCROLL_THRESHOLD = 32;

const useClientLayoutEffect = typeof window === "undefined"
  ? React.useEffect
  : React.useLayoutEffect;

export interface ScrollWindowOptions {
  /** 面板是否可见；不可见时不改动滚动位置。 */
  active: boolean;
  /** 切换会话时重置可见窗口并回到最新。 */
  sessionId: string;
  /** 当前可渲染的条目总数。 */
  itemCount: number;
  /** 首次进入时展示的条目数。 */
  initialVisibleCount: number;
  /** 每次向前展开的批次大小（仅客户端窗口）。 */
  olderBatchSize: number;
  /**
   * 客户端可见窗口已用尽（firstVisibleIndex 为 0）且用户继续向上滚动时的回调，
   * 用于事件视图向服务端请求更旧页；请求视图不提供该回调。
   */
  onExhaustedOlderItems?: () => void;
}

export interface ScrollWindow {
  firstVisibleIndex: number;
  handleListScroll: () => void;
  listRef: React.RefObject<HTMLDivElement>;
  /** 客户端窗口向前展开一批，并恢复滚动锚点；返回是否真的展开了。 */
  revealOlderItems: () => boolean;
  /** 服务端旧页返回后追加可见条目。 */
  appendVisibleCount: (added: number) => void;
  /** 记录当前滚动锚点，供异步加载后恢复。 */
  captureScrollAnchor: () => void;
  /** 服务端旧页请求失败或没有新增时放弃锚点。 */
  discardScrollAnchor: () => void;
}

/**
 * 诊断类面板共用的「可见窗口 + 滚动锚点」实现。
 * 请求视图与事件视图此前各自逐字实现同一套 refs、两个布局副作用和滚动处理，
 * 这里收敛为唯一实现；两处差异仅剩初始条数与批次大小两个参数。
 */
export function useScrollWindow({
  active,
  sessionId,
  itemCount,
  initialVisibleCount,
  olderBatchSize,
  onExhaustedOlderItems,
}: ScrollWindowOptions): ScrollWindow {
  const listRef = React.useRef<HTMLDivElement>(null);
  const restoreScrollRef = React.useRef<{ height: number; top: number } | null>(null);
  const shouldScrollToLatestRef = React.useRef(true);
  const stickToLatestRef = React.useRef(true);
  const [visibleCount, setVisibleCount] = React.useState(initialVisibleCount);
  const firstVisibleIndex = Math.max(itemCount - visibleCount, 0);

  useClientLayoutEffect(() => {
    if (!active) return;
    setVisibleCount(initialVisibleCount);
    shouldScrollToLatestRef.current = true;
    stickToLatestRef.current = true;
  }, [active, sessionId, initialVisibleCount]);

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
  }, [active, itemCount, sessionId, visibleCount]);

  const captureScrollAnchor = React.useCallback(() => {
    const list = listRef.current;
    if (!list) return;
    restoreScrollRef.current = { height: list.scrollHeight, top: list.scrollTop };
  }, []);

  const discardScrollAnchor = React.useCallback(() => {
    restoreScrollRef.current = null;
  }, []);

  const appendVisibleCount = React.useCallback((added: number) => {
    setVisibleCount((current) => current + added);
  }, []);

  const revealOlderItems = React.useCallback((): boolean => {
    const list = listRef.current;
    if (!list) return false;
    if (firstVisibleIndex <= 0) return false;
    restoreScrollRef.current = { height: list.scrollHeight, top: list.scrollTop };
    setVisibleCount((current) => Math.min(current + olderBatchSize, itemCount));
    return true;
  }, [firstVisibleIndex, itemCount, olderBatchSize]);

  const handleListScroll = React.useCallback(() => {
    const list = listRef.current;
    if (!list) return;
    stickToLatestRef.current =
      list.scrollHeight - list.scrollTop - list.clientHeight <= LOAD_OLDER_SCROLL_THRESHOLD;
    if (list.scrollTop > LOAD_OLDER_SCROLL_THRESHOLD) return;
    if (!revealOlderItems()) onExhaustedOlderItems?.();
  }, [onExhaustedOlderItems, revealOlderItems]);

  return {
    firstVisibleIndex,
    handleListScroll,
    listRef,
    revealOlderItems,
    appendVisibleCount,
    captureScrollAnchor,
    discardScrollAnchor,
  };
}

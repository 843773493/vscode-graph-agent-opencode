import type { LoadAroundTurn } from "../../api/session/sessionTurnHistory";
import { StaleTurnReferenceHttpError } from "../../api/session/sessionTurnHistory";
import React from "react";
import type { VirtuosoHandle } from "react-virtuoso";
import { errorMessage } from "../../utils/errorMessage";
import type { ChatTranscriptItem } from "./transcriptItems";
import type { TurnMessageMatchDTO, TurnMessageLocationDTO, TurnMessageReferenceDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";

export interface TurnViewportPosition {
  turn_anchor: string | null;
  scroll_offset: number;
  follow_latest: boolean;
}

interface TurnNavigation {
  scopeKey: string;
  turnId: string;
  phase: "loading" | "positioning" | "located" | "error";
  message: string;
}

interface TurnAnchor {
  scopeKey: string;
  turnId: string;
  offset: number;
  restoring: boolean;
  controller: AbortController;
  loading: boolean;
  located: boolean;
  message?: TurnMessageReferenceDTO;
}

function isTargetTurn(item: ChatTranscriptItem, turnId: string): boolean {
  return item.kind === "turn" && item.conversation.displayMode === "history"
    && !item.conversation.pending && item.conversation.turnId === turnId;
}

/** 用户跳转与保存位置恢复共用同一稳定 Turn 锚点；索引和尺寸只在定位时读取。 */
export function useTurnAnchorNavigation({
  items, scopeKey, streamRef, scrollerRef, followsLatestRef, setShowJumpToLatest,
  onLoadAroundTurn, historyLoading, viewState, onViewStateChange, onViewStateRestoreStatus,
  messageNavigation,
}: {
  items: readonly ChatTranscriptItem[];
  scopeKey: string;
  streamRef: React.MutableRefObject<VirtuosoHandle | null>;
  scrollerRef: React.MutableRefObject<HTMLElement | null>;
  followsLatestRef: React.MutableRefObject<boolean>;
  setShowJumpToLatest: (value: boolean) => void;
  onLoadAroundTurn: LoadAroundTurn;
  historyLoading: boolean;
  viewState?: TurnViewportPosition | null;
  onViewStateChange?: (position: TurnViewportPosition) => void;
  onViewStateRestoreStatus?: (message: string) => void;
  messageNavigation?: TurnMessageMatchDTO;
}) {
  const anchorRef = React.useRef<TurnAnchor | null>(null);
  const scopeRef = React.useRef(scopeKey);
  const restoredScopeRef = React.useRef<string | null>(null);
  const frameRef = React.useRef<number | null>(null);
  const [navigation, setNavigation] = React.useState<TurnNavigation | null>(null);
  const [locatedMessage, setLocatedMessage] = React.useState<{ scopeKey: string; message: TurnMessageLocationDTO }>();
  const handledMessageRef = React.useRef<TurnMessageMatchDTO>();
  const currentRef = React.useRef({ items, scopeKey, onViewStateChange, onViewStateRestoreStatus });
  currentRef.current = { items, scopeKey, onViewStateChange, onViewStateRestoreStatus };
  if (scopeRef.current !== scopeKey) {
    anchorRef.current?.controller.abort();
    anchorRef.current = null;
    scopeRef.current = scopeKey;
    restoredScopeRef.current = null;
    // 已取消的定位状态与任务一起失效，返回原 owner 时不能重新显示。
    setNavigation(null);
    setLocatedMessage(undefined);
    followsLatestRef.current = true;
  }

  const cancelAnchor = React.useCallback((clearMessage = false) => {
    anchorRef.current?.controller.abort();
    anchorRef.current = null;
    restoredScopeRef.current = currentRef.current.scopeKey;
    setNavigation(null);
    if (clearMessage) setLocatedMessage(undefined);
  }, []);
  const closeMessageNavigation = React.useCallback(() => cancelAnchor(true), [cancelAnchor]);

  const scrollToLatest = React.useCallback((behavior: "auto" | "smooth" = "auto", rememberIntent = true) => {
    if (rememberIntent) cancelAnchor(true);
    else if (anchorRef.current) return;
    followsLatestRef.current = true;
    setShowJumpToLatest(false);
    streamRef.current?.scrollToIndex({ index: "LAST", align: "end", behavior });
  }, [cancelAnchor, followsLatestRef, setShowJumpToLatest, streamRef]);

  React.useLayoutEffect(() => {
    setShowJumpToLatest(false);
  }, [scopeKey, setShowJumpToLatest]);

  const failAnchor = React.useCallback((anchor: TurnAnchor, message: string) => {
    if (anchorRef.current !== anchor || anchor.scopeKey !== currentRef.current.scopeKey) return;
    anchor.controller.abort();
    anchorRef.current = null;
    setLocatedMessage(undefined);
    if (anchor.restoring) {
      scrollToLatest();
      currentRef.current.onViewStateRestoreStatus?.("历史位置已失效，已回到会话尾部：" + message);
    }
    setNavigation({ scopeKey: anchor.scopeKey, turnId: anchor.turnId, phase: "error", message });
  }, [scrollToLatest]);

  const navigateToTurn = React.useCallback((turnId: string, offset = 0, restoring = false, message?: TurnMessageReferenceDTO) => {
    cancelAnchor(true);
    restoredScopeRef.current = scopeKey;
    followsLatestRef.current = false;
    setShowJumpToLatest(true);
    const anchor: TurnAnchor = {
      scopeKey, turnId: turnId.trim(), offset, restoring,
      controller: new AbortController(), loading: false, located: false, message,
    };
    anchorRef.current = anchor;
    if (!anchor.turnId) {
      failAnchor(anchor, "请输入轮次 ID");
      return;
    }
    const loaded = !message && currentRef.current.items.some((item) => isTargetTurn(item, anchor.turnId));
    anchor.loading = !loaded;
    setNavigation({ scopeKey, turnId: anchor.turnId, phase: loaded ? "positioning" : "loading",
      message: loaded ? "正在定位轮次…" : "正在读取指定轮次…" });
    if (loaded) return;
    void onLoadAroundTurn(anchor.turnId, anchor.controller.signal, message).then((located) => {
      if (anchorRef.current !== anchor || anchor.controller.signal.aborted || currentRef.current.scopeKey !== scopeKey) return;
      if (message && !located) { failAnchor(anchor, "后端未返回已核验的消息原文"); return; }
      setLocatedMessage(located ? { scopeKey, message: located } : undefined);
      anchor.loading = false;
      setNavigation({ scopeKey, turnId: anchor.turnId, phase: "positioning", message: "正在定位轮次…" });
    }, (error: unknown) => {
      failAnchor(anchor, message && error instanceof StaleTurnReferenceHttpError
        ? "消息命中已过期，请重新搜索" : "无法跳到指定轮次：" + errorMessage(error));
    });
  }, [cancelAnchor, failAnchor, followsLatestRef, onLoadAroundTurn, scopeKey, setShowJumpToLatest]);

  const alignAnchor = React.useCallback(() => {
    const anchor = anchorRef.current;
    if (!anchor || anchor.loading || anchor.scopeKey !== currentRef.current.scopeKey) return;
    const index = currentRef.current.items.findIndex((item) => isTargetTurn(item, anchor.turnId));
    if (index < 0) {
      failAnchor(anchor, "当前会话线程中未找到指定轮次：" + anchor.turnId);
      return;
    }
    const scroller = scrollerRef.current;
    const stream = streamRef.current;
    if (!scroller || !stream) return;
    const element = Array.from(scroller.querySelectorAll<HTMLElement>("[data-turn-id]"))
      .find((candidate) => candidate.dataset.turnId === anchor.turnId);
    if (!element) {
      // Virtuoso 的命令使用 data 相对索引，firstItemIndex 只负责前插身份。
      stream.scrollToIndex({ index, align: "start", behavior: "auto", offset: anchor.offset });
      return;
    }
    const targetElement = anchor.message
      ? Array.from(element.querySelectorAll<HTMLElement>("[data-message-item-id]"))
        .find(candidate => candidate.dataset.messageItemId === anchor.message!.item_id)
      : element;
    if (!targetElement) return;
    const viewport = scroller.getBoundingClientRect();
    const delta = targetElement.getBoundingClientRect().top - viewport.top + anchor.offset;
    if (Math.abs(delta) > 0.5) {
      scroller.scrollTop = Math.max(0, Math.min(scroller.scrollHeight - scroller.clientHeight, scroller.scrollTop + delta));
    }
    const bounds = targetElement.getBoundingClientRect();
    if (bounds.bottom <= viewport.top || bounds.top >= viewport.bottom || anchor.located) return;
    anchor.located = true;
    const target = currentRef.current.items[index];
    const ordinal = target.kind === "turn" ? target.conversation.turnOrdinal : undefined;
    setNavigation({ scopeKey: anchor.scopeKey, turnId: anchor.turnId, phase: "located",
      message: anchor.message ? "已定位消息原文" : ordinal === undefined ? "已定位指定轮次" : "已定位第 " + ordinal + " 轮" });
    currentRef.current.onViewStateChange?.({ turn_anchor: anchor.turnId, scroll_offset: anchor.offset, follow_latest: false });
  }, [failAnchor, scrollerRef, streamRef]);

  const scheduleAnchorAlignment = React.useCallback(() => {
    if (typeof window === "undefined" || !anchorRef.current || frameRef.current !== null) return;
    frameRef.current = window.requestAnimationFrame(() => {
      frameRef.current = null;
      alignAnchor();
    });
  }, [alignAnchor]);

  React.useLayoutEffect(() => {
    scheduleAnchorAlignment();
  }, [items, navigation, scheduleAnchorAlignment]);

  React.useEffect(() => {
    const anchor = anchorRef.current;
    if (!anchor || anchor.loading || anchor.located || typeof window === "undefined") return;
    const timer = window.setTimeout(() => {
      failAnchor(anchor, "指定轮次已加载，但无法在聊天视口中完成定位，请重试");
    }, 5000);
    return () => window.clearTimeout(timer);
  }, [failAnchor, navigation]);

  React.useEffect(() => {
    const anchor = anchorRef.current;
    const scroller = scrollerRef.current;
    if (!anchor?.located || !scroller || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(scheduleAnchorAlignment);
    observer.observe(scroller);
    for (const element of scroller.querySelectorAll<HTMLElement>(".chat-virtual-item")) {
      observer.observe(element);
    }
    return () => observer.disconnect();
  }, [items, navigation, scheduleAnchorAlignment, scrollerRef]);

  React.useEffect(() => {
    if (!messageNavigation || historyLoading || handledMessageRef.current === messageNavigation) return;
    handledMessageRef.current = messageNavigation;
    if (!messageNavigation.reference) {
      cancelAnchor();
      setNavigation({ scopeKey, turnId: messageNavigation.turn_id, phase: "error", message: "消息命中缺少位置引用，请重新查询" });
      return;
    }
    navigateToTurn(messageNavigation.turn_id, 0, false, messageNavigation.reference);
  }, [cancelAnchor, historyLoading, messageNavigation, navigateToTurn, scopeKey]);

  React.useEffect(() => {
    if (messageNavigation && handledMessageRef.current !== messageNavigation) return;
    if (!viewState || historyLoading || restoredScopeRef.current === scopeKey) return;
    restoredScopeRef.current = scopeKey;
    if (viewState.follow_latest || !viewState.turn_anchor) {
      scrollToLatest();
    } else {
      navigateToTurn(viewState.turn_anchor, viewState.scroll_offset, true);
    }
  }, [historyLoading, messageNavigation, navigateToTurn, scopeKey, scrollToLatest, viewState]);

  React.useEffect(() => () => {
    anchorRef.current?.controller.abort();
    anchorRef.current = null;
    if (frameRef.current !== null && typeof window !== "undefined") window.cancelAnimationFrame(frameRef.current);
  }, []);

  return {
    anchorRef, cancelAnchor, navigateToTurn, scheduleAnchorAlignment, scrollToLatest,
    closeMessageNavigation,
    locatedMessage: locatedMessage?.scopeKey === scopeKey ? locatedMessage.message : undefined,
    navigation: navigation?.scopeKey === scopeKey ? navigation : null,
  };
}

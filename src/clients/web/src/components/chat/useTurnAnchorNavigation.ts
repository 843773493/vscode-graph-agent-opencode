import type { LoadAroundTurn, TurnNavigationTarget, TurnItemNavigation } from "../../api/session/sessionTurnHistory";
import { StaleTurnReferenceHttpError } from "../../api/session/sessionTurnHistory";
import { formatNumber, t } from "../../../../shared/core/i18n/index.js";
import React from "react";
import type { VirtuosoHandle } from "react-virtuoso";
import { errorMessage } from "../../utils/errorMessage";
import type { ChatTranscriptItem } from "./transcriptItems";
import type { TurnToolResultDTO, TurnMessageLocationDTO, TurnMessageReferenceDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";

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
  tool?: TurnToolResultDTO;
  item?: TurnItemNavigation;
}

function isTargetTurn(item: ChatTranscriptItem, turnId: string): boolean {
  return item.kind === "turn" && item.conversation.displayMode === "history"
    && !item.conversation.pending && item.conversation.turnId === turnId;
}

/** 用户跳转与保存位置恢复共用同一稳定 Turn 锚点；索引和尺寸只在定位时读取。 */
export function useTurnAnchorNavigation({
  items, scopeKey, streamRef, scrollerRef, followsLatestRef,
  onLoadAroundTurn, historyLoading, viewState, onViewStateChange, onViewStateRestoreStatus,
  messageNavigation,
}: {
  items: readonly ChatTranscriptItem[];
  scopeKey: string;
  streamRef: React.MutableRefObject<VirtuosoHandle | null>;
  scrollerRef: React.MutableRefObject<HTMLElement | null>;
  followsLatestRef: React.MutableRefObject<boolean>;
  onLoadAroundTurn: LoadAroundTurn;
  historyLoading: boolean;
  viewState?: TurnViewportPosition | null;
  onViewStateChange?: (position: TurnViewportPosition) => void;
  onViewStateRestoreStatus?: (message: string) => void;
  messageNavigation?: TurnNavigationTarget;
}) {
  const anchorRef = React.useRef<TurnAnchor | null>(null);
  const scopeRef = React.useRef(scopeKey);
  const restoredScopeRef = React.useRef<string | null>(null);
  const frameRef = React.useRef<number | null>(null);
  const [navigation, setNavigation] = React.useState<TurnNavigation | null>(null);
  const [locatedItem, setLocatedItem] = React.useState<TurnItemNavigation>();
  const [located, setLocated] = React.useState<{ scopeKey: string; value: TurnMessageLocationDTO | TurnToolResultDTO }>();
  const handledMessageRef = React.useRef<TurnNavigationTarget>();
  const currentRef = React.useRef({ items, scopeKey, onViewStateChange, onViewStateRestoreStatus });
  currentRef.current = { items, scopeKey, onViewStateChange, onViewStateRestoreStatus };
  if (scopeRef.current !== scopeKey) {
    anchorRef.current?.controller.abort();
    anchorRef.current = null;
    scopeRef.current = scopeKey;
    restoredScopeRef.current = null;
    // 已取消的定位状态与任务一起失效，返回原 owner 时不能重新显示。
    setNavigation(null);
    setLocated(undefined);
    setLocatedItem(undefined);
    followsLatestRef.current = true;
  }

  const cancelAnchor = React.useCallback((clearMessage = false) => {
    anchorRef.current?.controller.abort();
    anchorRef.current = null;
    restoredScopeRef.current = currentRef.current.scopeKey;
    setNavigation(null);
    if (clearMessage) { setLocated(undefined); setLocatedItem(undefined); }
  }, []);
  const closeMessageNavigation = React.useCallback(() => cancelAnchor(true), [cancelAnchor]);

  const scrollToLatest = React.useCallback((behavior: "auto" | "smooth" = "auto", rememberIntent = true) => {
    if (rememberIntent) cancelAnchor(true);
    else if (anchorRef.current) return;
    followsLatestRef.current = true;
    streamRef.current?.scrollToIndex({ index: "LAST", align: "end", behavior });
  }, [cancelAnchor, followsLatestRef, streamRef]);

  const failAnchor = React.useCallback((anchor: TurnAnchor, message: string) => {
    if (anchorRef.current !== anchor || anchor.scopeKey !== currentRef.current.scopeKey) return;
    anchor.controller.abort();
    anchorRef.current = null;
    setLocated(undefined);
    setLocatedItem(undefined);
    if (anchor.restoring) {
      scrollToLatest();
      currentRef.current.onViewStateRestoreStatus?.(t("chat.turnNavigation.restoreFailed", message));
    }
    setNavigation({ scopeKey: anchor.scopeKey, turnId: anchor.turnId, phase: "error", message });
  }, [scrollToLatest]);

  const navigateToTurn = React.useCallback((turnId: string, offset = 0, restoring = false, message?: TurnMessageReferenceDTO, tool?: TurnToolResultDTO, item?: TurnItemNavigation) => {
    cancelAnchor(true);
    restoredScopeRef.current = scopeKey;
    followsLatestRef.current = false;
    const anchor: TurnAnchor = {
      scopeKey, turnId: turnId.trim(), offset, restoring,
      controller: new AbortController(), loading: false, located: false, message, tool, item,
    };
    anchorRef.current = anchor;
    if (!anchor.turnId) {
      failAnchor(anchor, t("chat.turnNavigation.emptyTurnId"));
      return;
    }
    const loaded = !message && !tool && !item && currentRef.current.items.some((item) => isTargetTurn(item, anchor.turnId));
    anchor.loading = !loaded;
    setNavigation({ scopeKey, turnId: anchor.turnId, phase: loaded ? "positioning" : "loading",
      message: loaded ? t("chat.turnNavigation.positioning") : t("chat.turnNavigation.loading") });
    if (loaded) return;
    void onLoadAroundTurn(anchor.turnId, anchor.controller.signal, message, tool?.item_id, item).then((located) => {
      if (anchorRef.current !== anchor || anchor.controller.signal.aborted || currentRef.current.scopeKey !== scopeKey) return;
      if (message && (!located || !("reference" in located))) { failAnchor(anchor, t("chat.turnNavigation.messageMissing")); return; }
      if (tool && (!located || !("item_id" in located) || located.item_id !== tool.item_id)) { failAnchor(anchor, t("chat.turnNavigation.toolResultMissing")); return; }
      setLocatedItem(item);
      setLocated(located ? { scopeKey, value: located } : undefined);
      anchor.loading = false;
      setNavigation({ scopeKey, turnId: anchor.turnId, phase: "positioning", message: t("chat.turnNavigation.positioning") });
    }, (error: unknown) => {
      failAnchor(anchor, (message || tool) && error instanceof StaleTurnReferenceHttpError
        ? tool ? t("chat.turnNavigation.staleToolResult") : t("chat.turnNavigation.staleMessage")
        : t("chat.turnNavigation.failed", errorMessage(error)));
    });
  }, [cancelAnchor, failAnchor, followsLatestRef, onLoadAroundTurn, scopeKey]);

  const alignAnchor = React.useCallback(() => {
    const anchor = anchorRef.current;
    if (!anchor || anchor.loading || anchor.scopeKey !== currentRef.current.scopeKey) return;
    const index = currentRef.current.items.findIndex((item) => isTargetTurn(item, anchor.turnId));
    if (index < 0) {
      failAnchor(anchor, t("chat.turnNavigation.notFound", anchor.turnId));
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
    const itemElement = anchor.item ? Array.from(element.querySelectorAll<HTMLElement>("[data-response-sources], [data-chat-item-id]"))
      .find(candidate => candidate.dataset.chatItemId === anchor.item!.message_id
        || candidate.dataset.chatItemId === anchor.item!.item_id
        || candidate.dataset.responseSources && (JSON.parse(candidate.dataset.responseSources) as Array<{ source?: { item_id?: string } }>)
          .some(part => part.source?.item_id === anchor.item!.item_id)) : undefined;
    const targetElement = anchor.item ? itemElement?.closest<HTMLElement>(".chat-activity-item") ?? itemElement : anchor.message
      ? Array.from(element.querySelectorAll<HTMLElement>("[data-message-item-id]"))
        .find(candidate => candidate.dataset.messageItemId === anchor.message!.item_id)
      : anchor.tool ? Array.from(element.querySelectorAll<HTMLElement>("[data-tool-result-item-id]"))
        .find(candidate => candidate.dataset.toolResultItemId === anchor.tool!.item_id) : element;
    if (!targetElement) return;
    // 工具详情本身限高并滚动；先揭示内层命中，再对齐外层虚拟列表。
    if (anchor.message || anchor.tool || anchor.item) targetElement.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "auto" });
    const viewport = scroller.getBoundingClientRect();
    const delta = targetElement.getBoundingClientRect().top - viewport.top + anchor.offset;
    if (Math.abs(delta) > 0.5) {
      scroller.scrollTop = Math.max(0, Math.min(scroller.scrollHeight - scroller.clientHeight, scroller.scrollTop + delta));
    }
    const bounds = targetElement.getBoundingClientRect();
    if (bounds.bottom <= viewport.top || bounds.top >= viewport.bottom || anchor.located) return;
    anchor.located = true;
    // 仅强调主动跳转的具体 Item／命中；动画结束不移除正文，也不影响滚动锚点。
    if (!anchor.restoring && (anchor.message || anchor.tool || anchor.item)) {
      targetElement.animate([
        { backgroundColor: "var(--vscode-editor-findMatchBackground, #b5890040)",
          outline: "1px solid var(--vscode-editor-findMatchBorder, var(--bt-link-foreground))", offset: 0 },
        { backgroundColor: "var(--vscode-editor-findMatchBackground, #b5890040)",
          outline: "1px solid var(--vscode-editor-findMatchBorder, var(--bt-link-foreground))", offset: 0.6 },
        { backgroundColor: "transparent", outline: "1px solid transparent" },
      ], { duration: 1800 });
    }
    const target = currentRef.current.items[index];
    const ordinal = target.kind === "turn" ? target.conversation.turnOrdinal : undefined;
    setNavigation({ scopeKey: anchor.scopeKey, turnId: anchor.turnId, phase: "located",
      message: anchor.tool ? t("chat.turnNavigation.locatedToolResult")
        : anchor.message ? t("chat.turnNavigation.locatedMessage")
          : ordinal === undefined ? t("chat.turnNavigation.locatedTurn")
            : t("chat.turnNavigation.locatedOrdinal", formatNumber(ordinal)) });
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
      failAnchor(anchor, t("chat.turnNavigation.positioningFailed"));
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
    if ("semantic_kind" in messageNavigation) {
      navigateToTurn(messageNavigation.turn_id, 0, false, undefined, undefined, messageNavigation);
    } else if ("item_id" in messageNavigation) {
      navigateToTurn(messageNavigation.turn_id, 0, false, undefined, messageNavigation);
    } else {
      if (!messageNavigation.reference) {
        cancelAnchor();
        setNavigation({ scopeKey, turnId: messageNavigation.turn_id, phase: "error", message: t("chat.turnNavigation.messageReferenceMissing") });
        return;
      }
      navigateToTurn(messageNavigation.turn_id, 0, false, messageNavigation.reference);
    }
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
    locatedMessage: located?.scopeKey === scopeKey && "reference" in located.value ? located.value : undefined,
    locatedItem,
    locatedTool: located?.scopeKey === scopeKey && "item_id" in located.value ? located.value : undefined,
    // 恢复保留内部定位状态以持续校正布局，但成功与加载过程不打扰阅读；错误仍展示。
    navigation: navigation?.scopeKey === scopeKey && !anchorRef.current?.restoring ? navigation : null,
  };
}

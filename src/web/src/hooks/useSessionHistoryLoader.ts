import { useCallback, useEffect } from "react";
import { listMessages } from "../api";
import { listPendingRequests } from "../pendingRequestsApi";
import { cloneMaps } from "../state/appStateMaps";
import { updateAttachmentSummariesFromMessages } from "../state/attachments";
import { appendFrontendEvent } from "../state/traceEvents";
import { writePendingSnapshot } from "../state/conversations";
import type { SetAppState } from "./contentViewLoaderTypes";
import type { Message } from "../types/backend";

interface CachedHistoryPage {
  messages: Message[];
  nextCursor: string | null;
  hasMore: boolean;
}

const HISTORY_CACHE_LIMIT = 8;
const historyCache = new Map<string, CachedHistoryPage>();

function cacheKey(workspaceId: string | null, sessionId: string): string {
  return `${workspaceId ?? "local"}::${sessionId}`;
}

function writeHistoryCache(key: string, page: CachedHistoryPage): void {
  historyCache.delete(key);
  historyCache.set(key, page);
  while (historyCache.size > HISTORY_CACHE_LIMIT) {
    const oldestKey = historyCache.keys().next().value;
    if (typeof oldestKey !== "string") break;
    historyCache.delete(oldestKey);
  }
}

export function useSessionHistoryLoader({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  reloadNonce,
  setState,
}: {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  reloadNonce: number;
  setState: SetAppState;
}): { loadOlderMessages: () => Promise<void> } {
  useEffect(() => {
    if (!apiPort || !sessionId) return;
    const targetWorkspaceId = workspaceId;
    const targetSessionCacheKey = sessionCacheKey ?? sessionId;
    const targetHistoryCacheKey = cacheKey(workspaceId, sessionId);
    const cachedPage = historyCache.get(targetHistoryCacheKey);

    let cancelled = false;
    const controller = new AbortController();
    setState((prev) => {
      if (prev.currentSession?.session_id !== sessionId) return prev;
      const next = cloneMaps(prev);
      next.messageHistoryNextCursor = null;
      next.messageHistoryHasMore = false;
      next.messageHistoryLoadingOlder = false;
      next.messageHistoryError = null;
      if (cachedPage) {
        next.messages = cachedPage.messages;
        next.messageHistoryNextCursor = cachedPage.nextCursor;
        next.messageHistoryHasMore = cachedPage.hasMore;
        next.status = "已显示会话缓存，正在校准最新消息";
      }
      appendFrontendEvent(
        next.eventQueuesBySession,
        sessionId,
        "session_load_started",
        "开始加载会话历史",
        { session_id: sessionId },
        "",
        targetSessionCacheKey,
      );
      return next;
    });

    const timerId = window.setTimeout(() => void (async () => {
      try {
        const [messages, pendingSnapshot] = await Promise.all([
          listMessages(apiPort, sessionId, targetWorkspaceId, {
            signal: controller.signal,
          }),
          listPendingRequests(apiPort, sessionId, targetWorkspaceId),
        ]);
        if (cancelled) return;
        writeHistoryCache(targetHistoryCacheKey, {
          messages: messages.items ?? [],
          nextCursor: messages.next_cursor ?? null,
          hasMore: messages.has_more ?? false,
        });
        setState((prev) => {
          if (prev.currentSession?.session_id !== sessionId) return prev;
          if (
            targetWorkspaceId &&
            prev.currentSessionWorkspaceId !== targetWorkspaceId
          ) {
            return prev;
          }
          const next = cloneMaps(prev);
          next.messages = messages.items ?? [];
          next.messageHistoryNextCursor = messages.next_cursor ?? null;
          next.messageHistoryHasMore = messages.has_more ?? false;
          next.messageHistoryError = null;
          writePendingSnapshot(
            next.pendingConversations,
            next.activeJobIdsBySession,
            pendingSnapshot,
            targetSessionCacheKey,
          );
          updateAttachmentSummariesFromMessages(
            next.sessionAttachmentSummaries,
            next.messages,
          );
          appendFrontendEvent(
            next.eventQueuesBySession,
            sessionId,
            "session_load_completed",
            "会话历史加载完成",
            {
              session_id: sessionId,
              message_count: messages.items?.length ?? 0,
            },
            "",
            targetSessionCacheKey,
          );
          next.status = "会话历史加载完成";
          return next;
        });
      } catch (error) {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => {
          if (prev.currentSession?.session_id !== sessionId) {
            return prev;
          }
          const next = cloneMaps(prev);
          next.messageHistoryError = message;
          next.status = `加载失败: ${message}`;
          appendFrontendEvent(
            next.eventQueuesBySession,
            sessionId,
            "session_load_failed",
            "会话历史加载失败",
            { session_id: sessionId, error: message },
            message,
            targetSessionCacheKey,
          );
          return next;
        });
      }
    })(), 120);

    return () => {
      cancelled = true;
      controller.abort();
      window.clearTimeout(timerId);
    };
  }, [
    apiPort,
    reloadNonce,
    sessionCacheKey,
    sessionId,
    setState,
    workspaceId,
  ]);

  const loadOlderMessages = useCallback(async () => {
    if (!apiPort || !sessionId) return;
    let cursor: string | null = null;
    let shouldLoad = false;
    setState((prev) => {
      if (
        prev.currentSession?.session_id !== sessionId
        || prev.messageHistoryLoadingOlder
        || !prev.messageHistoryHasMore
        || !prev.messageHistoryNextCursor
      ) {
        return prev;
      }
      cursor = prev.messageHistoryNextCursor;
      shouldLoad = true;
      return {
        ...prev,
        messageHistoryLoadingOlder: true,
        messageHistoryError: null,
      };
    });
    if (!shouldLoad || !cursor) return;

    try {
      const page = await listMessages(apiPort, sessionId, workspaceId, {
        cursor,
        limit: 40,
      });
      setState((prev) => {
        if (
          prev.currentSession?.session_id !== sessionId
          || (workspaceId && prev.currentSessionWorkspaceId !== workspaceId)
        ) {
          return prev;
        }
        const knownIds = new Set(prev.messages.map((message) => message.message_id));
        const older = page.items.filter((message) => !knownIds.has(message.message_id));
        return {
          ...prev,
          messages: [...older, ...prev.messages],
          messageHistoryNextCursor: page.next_cursor ?? null,
          messageHistoryHasMore: page.has_more ?? false,
          messageHistoryLoadingOlder: false,
          messageHistoryError: null,
        };
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((prev) => (
        prev.currentSession?.session_id === sessionId
          ? {
              ...prev,
              messageHistoryLoadingOlder: false,
              messageHistoryError: message,
            }
          : prev
      ));
      throw error;
    }
  }, [apiPort, sessionId, setState, workspaceId]);

  return { loadOlderMessages };
}

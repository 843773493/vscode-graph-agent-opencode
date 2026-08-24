import { useEffect, useRef } from "react";
import {
  listSessionActivity,
  SessionActivityCursorGoneError,
  streamSessionActivity,
} from "../api/sessionActivity";
import { cloneMaps } from "../state/appStateMaps";
import { sessionScopeKey } from "../state/session/sessionScope";
import type { SessionActivity } from "../types/backend";
import type { SetAppState } from "./contentViewLoaderTypes";
import { refreshWorkspaceSessionList } from "./sessionEventStream/sessionRefresh";
import { sessionStreamReconnectDelay } from "./sessionEventStreamPolicy";
import { waitForReconnect } from "./waitForReconnect";

function markActivity(
  event: SessionActivity,
  workspaceId: string,
  currentSessionCacheKey: string | null,
  setState: SetAppState,
): void {
  const cacheKey = sessionScopeKey(workspaceId, event.session_id);
  setState((previous) => {
    if (
      cacheKey === currentSessionCacheKey
      && document.visibilityState === "visible"
      && document.hasFocus()
    ) {
      return previous;
    }
    const next = cloneMaps(previous);
    next.unreadSessionKeys.add(cacheKey);
    return next;
  });
}

export function useWorkspaceSessionActivity({
  apiPort,
  workspaceId,
  currentSessionCacheKey,
  setState,
}: {
  apiPort: number | null;
  workspaceId: string | null;
  currentSessionCacheKey: string | null;
  setState: SetAppState;
}): void {
  const currentSessionCacheKeyRef = useRef(currentSessionCacheKey);
  currentSessionCacheKeyRef.current = currentSessionCacheKey;

  useEffect(() => {
    if (!apiPort || !workspaceId) return;
    const controller = new AbortController();
    let cursor = 0;
    let reconnectAttempt = 0;
    let refreshPending = false;
    let refreshTimer: number | null = null;
    const seenEventIds = new Set<string>();

    const scheduleSessionRefresh = () => {
      refreshPending = true;
      if (refreshTimer !== null) return;
      refreshTimer = window.setTimeout(() => {
        refreshTimer = null;
        if (!refreshPending || controller.signal.aborted) return;
        refreshPending = false;
        void refreshWorkspaceSessionList(apiPort, workspaceId, setState).catch(
          (error: unknown) => {
            const message = error instanceof Error ? error.message : String(error);
            setState((previous) => ({
              ...previous,
              status: `刷新会话摘要失败: ${message}`,
            }));
          },
        );
      }, 120);
    };

    const receive = (event: SessionActivity, nextCursor: number) => {
      if (seenEventIds.has(event.event_id)) return;
      seenEventIds.add(event.event_id);
      cursor = Math.max(cursor, nextCursor);
      markActivity(
        event,
        workspaceId,
        currentSessionCacheKeyRef.current,
        setState,
      );
      scheduleSessionRefresh();
    };

    const connect = async () => {
      try {
        const initial = await listSessionActivity(apiPort, workspaceId, {
          limit: 1000,
        });
        const lastInitialEvent = initial.items[initial.items.length - 1];
        cursor = Number(initial.next_cursor ?? lastInitialEvent?.event_seq ?? 0);
        await streamSessionActivity(apiPort, workspaceId, {
          after: cursor,
          signal: controller.signal,
          onEvent: receive,
          onActivity: () => {
            reconnectAttempt = 0;
          },
        });
        if (!controller.signal.aborted) {
          throw new Error("会话活动流已关闭");
        }
      } catch (error: unknown) {
        if (controller.signal.aborted) return;
        if (error instanceof SessionActivityCursorGoneError) {
          cursor = 0;
          try {
            await refreshWorkspaceSessionList(apiPort, workspaceId, setState);
          } catch (refreshError: unknown) {
            const message = refreshError instanceof Error
              ? refreshError.message
              : String(refreshError);
            setState((previous) => ({
              ...previous,
              status: `活动游标失效且会话摘要刷新失败: ${message}`,
            }));
          }
          reconnectAttempt = 0;
        } else {
          const message = error instanceof Error ? error.message : String(error);
          setState((previous) => ({
            ...previous,
            status: `会话活动流断开，正在重连: ${message}`,
          }));
        }
        if (!controller.signal.aborted) {
          await waitForReconnect(
            controller.signal,
            sessionStreamReconnectDelay(reconnectAttempt),
          );
          reconnectAttempt += 1;
          void connect();
        }
      }
    };

    void connect();
    return () => {
      controller.abort();
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
    };
  }, [apiPort, workspaceId, setState]);
}

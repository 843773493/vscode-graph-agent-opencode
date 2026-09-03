import React from "react";
import {
  listSessionTraceHistory,
  TraceCursorGoneError,
} from "../../api/sessionTraceStream";
import { dedupeTraceEvents } from "../../state/traceEvents";
import type { Session } from "../../types/backend";
import type { SessionTraceHistoryState } from "../../types/frontend";
import type { SetAppState } from "../contentViewLoaderTypes";

const SESSION_TRACE_HISTORY_CACHE_LIMIT = 8;

function writeHistoryCache(
  current: Map<string, SessionTraceHistoryState>,
  scopeKey: string,
  history: SessionTraceHistoryState,
): Map<string, SessionTraceHistoryState> {
  const next = new Map(current);
  next.delete(scopeKey);
  next.set(scopeKey, history);
  while (next.size > SESSION_TRACE_HISTORY_CACHE_LIMIT) {
    const oldest = next.keys().next().value;
    if (typeof oldest !== "string") break;
    next.delete(oldest);
  }
  return next;
}

function emptyHistory(scopeKey: string, generation: number): SessionTraceHistoryState {
  return {
    scopeKey,
    generation,
    items: [],
    nextCursor: null,
    hasMore: false,
    loading: true,
    loadingOlder: false,
    error: null,
  };
}

export function useSessionTraceHistory({
  apiPort,
  currentSession,
  workspaceId,
  scopeKey,
  active,
  history,
  setState,
}: {
  apiPort: number;
  currentSession: Session | null;
  workspaceId: string | null;
  scopeKey: string | null;
  active: boolean;
  history: SessionTraceHistoryState | null;
  setState: SetAppState;
}) {
  const sessionId = currentSession?.session_id ?? null;
  const generationRef = React.useRef(0);
  const abortRef = React.useRef<AbortController | null>(null);
  const historyRef = React.useRef(history);
  historyRef.current = history;

  const refresh = React.useCallback(async (): Promise<void> => {
    if (!sessionId || !scopeKey) return;
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setState((previous) => {
      const map = writeHistoryCache(
        previous.sessionTraceHistoryBySession,
        scopeKey,
        emptyHistory(scopeKey, generation),
      );
      return { ...previous, sessionTraceHistoryBySession: map };
    });
    try {
      const page = await listSessionTraceHistory(
        apiPort,
        sessionId,
        workspaceId,
        { limit: 100, signal: controller.signal },
      );
      if (controller.signal.aborted || generationRef.current !== generation) return;
      setState((previous) => {
        const current = previous.sessionTraceHistoryBySession.get(scopeKey);
        if (current?.generation !== generation) return previous;
        const map = writeHistoryCache(previous.sessionTraceHistoryBySession, scopeKey, {
          ...current,
          items: dedupeTraceEvents(page.items),
          nextCursor: page.next_cursor ?? null,
          hasMore: page.has_more ?? Boolean(page.next_cursor),
          loading: false,
          error: null,
        });
        return { ...previous, sessionTraceHistoryBySession: map };
      });
    } catch (error) {
      if (controller.signal.aborted || generationRef.current !== generation) return;
      const message = error instanceof Error ? error.message : String(error);
      setState((previous) => {
        const current = previous.sessionTraceHistoryBySession.get(scopeKey);
        if (current?.generation !== generation) return previous;
        const map = writeHistoryCache(
          previous.sessionTraceHistoryBySession,
          scopeKey,
          { ...current, loading: false, error: message },
        );
        return {
          ...previous,
          sessionTraceHistoryBySession: map,
          status: `读取事件历史失败: ${message}`,
        };
      });
    }
  }, [apiPort, scopeKey, sessionId, setState, workspaceId]);

  React.useEffect(() => {
    if (!active || !sessionId || !scopeKey) {
      abortRef.current?.abort();
      return;
    }
    const cachedHistory = historyRef.current;
    if (cachedHistory?.scopeKey === scopeKey && !cachedHistory.loading && !cachedHistory.error) {
      return;
    }
    void refresh();
    return () => abortRef.current?.abort();
  }, [active, refresh, scopeKey, sessionId]);

  const loadOlder = React.useCallback(async (): Promise<number> => {
    if (
      !active
      || !sessionId
      || !scopeKey
      || !history
      || history.loading
      || history.loadingOlder
      || !history.hasMore
      || !history.nextCursor
    ) {
      return 0;
    }
    const generation = history.generation;
    const cursor = history.nextCursor;
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    setState((previous) => {
      const current = previous.sessionTraceHistoryBySession.get(scopeKey);
      if (current?.generation !== generation) return previous;
      const map = writeHistoryCache(
        previous.sessionTraceHistoryBySession,
        scopeKey,
        { ...current, loadingOlder: true, error: null },
      );
      return { ...previous, sessionTraceHistoryBySession: map };
    });
    try {
      const page = await listSessionTraceHistory(
        apiPort,
        sessionId,
        workspaceId,
        { cursor, limit: 100, signal: controller.signal },
      );
      if (controller.signal.aborted || generationRef.current !== generation) return 0;
      let added = 0;
      setState((previous) => {
        const current = previous.sessionTraceHistoryBySession.get(scopeKey);
        if (current?.generation !== generation || current.nextCursor !== cursor) return previous;
        const items = dedupeTraceEvents([...page.items, ...current.items]);
        added = items.length - current.items.length;
        const map = writeHistoryCache(previous.sessionTraceHistoryBySession, scopeKey, {
          ...current,
          items,
          nextCursor: page.next_cursor ?? null,
          hasMore: page.has_more ?? Boolean(page.next_cursor),
          loadingOlder: false,
          error: null,
        });
        return { ...previous, sessionTraceHistoryBySession: map };
      });
      return added;
    } catch (error) {
      if (controller.signal.aborted || generationRef.current !== generation) return 0;
      const detail = error instanceof TraceCursorGoneError
        ? `Trace 历史游标已失效，请重新加载：${error.cursor}`
        : error instanceof Error ? error.message : String(error);
      setState((previous) => {
        const current = previous.sessionTraceHistoryBySession.get(scopeKey);
        if (current?.generation !== generation) return previous;
        const map = writeHistoryCache(
          previous.sessionTraceHistoryBySession,
          scopeKey,
          { ...current, loadingOlder: false, error: detail },
        );
        return {
          ...previous,
          sessionTraceHistoryBySession: map,
          status: `读取更早事件失败: ${detail}`,
        };
      });
      return 0;
    }
  }, [active, apiPort, history, scopeKey, sessionId, setState, workspaceId]);

  return { loadOlder, refresh };
}

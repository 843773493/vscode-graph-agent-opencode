import { useCallback, type MutableRefObject } from "react";
import { HttpRequestError, isTransientNetworkError } from "../../api/http";
import {
  loadSessionHistory,
  StaleTurnCursorHttpError,
  StaleTurnReferenceHttpError,
} from "../../api/sessionTurnHistory";
import {
  applyTurnHistoryPage,
  createSessionTurnTimeline,
  decideTurnProjectionEpoch,
  failTurnTimeline,
  writeTurnTimelineCache,
  type SessionTurnTimeline,
} from "../../state/session/turnTimeline";
import type { SetAppState } from "../contentViewLoaderTypes";

type TurnPageDirection = "before" | "after";

const INITIAL_HISTORY_TURNS = 5;
const CURSOR_HISTORY_TURNS = 3;
const INITIAL_HISTORY_RETRY_DELAYS_MS = [100, 250, 500, 1000, 2000] as const;

function timelineForScope(
  timelines: Map<string, SessionTurnTimeline>,
  scopeKey: string,
): SessionTurnTimeline {
  return timelines.get(scopeKey) ?? createSessionTurnTimeline(scopeKey);
}

function pageCursor(
  timeline: SessionTurnTimeline,
  direction: TurnPageDirection,
): string | null {
  return direction === "before"
    ? timeline.beforeCursor ?? timeline.olderCursor
    : timeline.afterCursor;
}

function pageHasMore(
  timeline: SessionTurnTimeline,
  direction: TurnPageDirection,
): boolean {
  return direction === "before" ? timeline.hasBefore || timeline.hasMore : timeline.hasAfter;
}

function loadingPatch(
  direction: TurnPageDirection,
  loading: boolean,
): Partial<SessionTurnTimeline> {
  return direction === "before"
    ? { loadingBefore: loading, loadingOlder: loading }
    : { loadingAfter: loading };
}

function isLoading(
  timeline: SessionTurnTimeline,
  direction: TurnPageDirection,
): boolean {
  return direction === "before" ? timeline.loadingBefore : timeline.loadingAfter;
}

async function waitForInitialHistoryRetry(
  delayMs: number,
  signal: AbortSignal,
): Promise<boolean> {
  if (signal.aborted) return false;
  await new Promise<void>((resolve) => {
    const timer = globalThis.setTimeout(resolve, delayMs);
    signal.addEventListener(
      "abort",
      () => {
        globalThis.clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
  return !signal.aborted;
}

function isInitialHistoryRetryableError(error: unknown): boolean {
  if (error instanceof StaleTurnCursorHttpError) {
    return true;
  }
  if (error instanceof StaleTurnReferenceHttpError) return false;
  if (isTransientNetworkError(error)) return true;
  return error instanceof HttpRequestError
    && (error.status === 404 || error.status === 409);
}

function preserveTimelineAfterTransientNetworkFailure(
  timeline: SessionTurnTimeline,
  targetGeneration: number,
): SessionTurnTimeline {
  const hasVisibleContent = timeline.orderedTurnIds.length > 0;
  return {
    ...timeline,
    phase: hasVisibleContent ? timeline.phase : "error",
    loadingBefore: false,
    loadingAfter: false,
    loadingOlder: false,
    error: hasVisibleContent
      ? null
      : "历史服务暂时断开，当前没有可显示的历史；请稍后重试",
    generation: targetGeneration,
  };
}

export function useInitialTurnLoader({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  generationRef,
  requestSignal,
  setState,
  onMissingTurn,
}: {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  generationRef: MutableRefObject<number>;
  requestSignal: AbortSignal;
  setState: SetAppState;
  onMissingTurn: (turnIds: string[]) => void;
}): (latestTurnId?: string) => Promise<void> {
  return useCallback(async (latestTurnId?: string) => {
    if (!apiPort || !sessionId || !sessionCacheKey) return;
    const targetGeneration = generationRef.current;
    setState((previous) => {
      const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
      if (timeline.generation !== targetGeneration) return previous;
      return {
        ...previous,
        turnTimelinesBySession: writeTurnTimelineCache(
          previous.turnTimelinesBySession,
          sessionCacheKey,
          {
            ...timeline,
            loadingBefore: true,
            loadingOlder: true,
            error: null,
          },
        ),
      };
    });

    try {
      let page: Awaited<ReturnType<typeof loadSessionHistory>> | null = null;
      for (
        let attempt = 0;
        attempt <= INITIAL_HISTORY_RETRY_DELAYS_MS.length;
        attempt += 1
      ) {
        try {
          page = await loadSessionHistory(
            apiPort,
            sessionId,
            {
              // 首次加载不知道历史长度，直接从尾部取固定窗口，不伪造 cursor。
              direction: "tail",
              turns: INITIAL_HISTORY_TURNS,
            },
            workspaceId,
            requestSignal,
          );
          break;
        } catch (error) {
          if (
            !isInitialHistoryRetryableError(error)
            || attempt >= INITIAL_HISTORY_RETRY_DELAYS_MS.length
          ) {
            throw error;
          }
          const shouldContinue = await waitForInitialHistoryRetry(
            INITIAL_HISTORY_RETRY_DELAYS_MS[attempt],
            requestSignal,
          );
          if (!shouldContinue) return;
        }
      }
      if (page === null) {
        throw new Error("Turn 首次历史请求未返回结果");
      }
      if (requestSignal.aborted || generationRef.current !== targetGeneration) return;
      setState((previous) => {
        const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
        if (timeline.generation !== targetGeneration) return previous;
        const epochDecision = decideTurnProjectionEpoch(
          timeline.projectionEpoch,
          page.projection_epoch,
        );
        if (epochDecision === "discard_older") {
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              { ...timeline, loadingBefore: false, loadingOlder: false },
            ),
          };
        }
        if (epochDecision === "refresh_bootstrap") {
          return {
            ...previous,
            sessionHistoryReloadNonce: previous.sessionHistoryReloadNonce + 1,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              { ...timeline, loadingBefore: false, loadingOlder: false },
            ),
            status: "Turn 投影已更新，正在重新加载",
          };
        }
        return {
          ...previous,
          turnTimelinesBySession: writeTurnTimelineCache(
            previous.turnTimelinesBySession,
            sessionCacheKey,
            applyTurnHistoryPage(timeline, page, "before"),
          ),
        };
      });
    } catch (error) {
      if (requestSignal.aborted) {
        setState((previous) => {
          const timeline = timelineForScope(
            previous.turnTimelinesBySession,
            sessionCacheKey,
          );
          if (timeline.generation !== targetGeneration) return previous;
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              { ...timeline, loadingBefore: false, loadingOlder: false },
            ),
          };
        });
        return;
      }
      if (error instanceof StaleTurnCursorHttpError) {
        setState((previous) => {
          const timeline = timelineForScope(
            previous.turnTimelinesBySession,
            sessionCacheKey,
          );
          if (timeline.generation !== targetGeneration) return previous;
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              {
                ...timeline,
                loadingBefore: false,
                loadingOlder: false,
                error: null,
              },
            ),
            status: "Turn 历史正在提交，已保留当前回合，稍后重试",
          };
        });
        return;
      }
      if (error instanceof StaleTurnReferenceHttpError) {
        // stale_turn_reference 表示旧缓存位置已失效，不是写锁。
        // bootstrap 会在发送/刷新后重建时间线，不能重复请求同一个旧位置。
        setState((previous) => {
          const timeline = timelineForScope(
            previous.turnTimelinesBySession,
            sessionCacheKey,
          );
          if (timeline.generation !== targetGeneration) return previous;
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              {
                ...timeline,
                loadingBefore: false,
                loadingOlder: false,
                error: null,
              },
            ),
            status: "历史位置已失效，正在使用最新上下文重新加载",
          };
        });
        return;
      }
      if (error instanceof HttpRequestError && error.status === 409) {
        setState((previous) => {
          const timeline = timelineForScope(
            previous.turnTimelinesBySession,
            sessionCacheKey,
          );
          if (timeline.generation !== targetGeneration) return previous;
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              {
                ...timeline,
                loadingBefore: false,
                loadingOlder: false,
                error: null,
              },
            ),
            status: "Turn 历史正在提交，已保留当前回合，稍后重试",
          };
        });
        return;
      }
      if (isTransientNetworkError(error)) {
        setState((previous) => {
          const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
          if (timeline.generation !== targetGeneration) return previous;
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              preserveTimelineAfterTransientNetworkFailure(
                timeline,
                targetGeneration,
              ),
            ),
            status: "历史连接暂时变化，已保留当前内容，可继续重试",
          };
        });
        return;
      }
      if (error instanceof HttpRequestError && error.status === 404 && latestTurnId) {
        onMissingTurn([latestTurnId]);
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      setState((previous) => {
        const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
        if (timeline.generation !== targetGeneration) return previous;
        return {
          ...previous,
          turnTimelinesBySession: writeTurnTimelineCache(
            previous.turnTimelinesBySession,
            sessionCacheKey,
            failTurnTimeline(timeline, targetGeneration, message),
          ),
        };
      });
      throw error;
    }
  }, [
    apiPort,
    generationRef,
    onMissingTurn,
    requestSignal,
    sessionCacheKey,
    sessionId,
    setState,
    workspaceId,
  ]);
}

export function useDirectionalTurnLoader({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  getCurrentTimeline,
  generationRef,
  requestSignal,
  setState,
  direction,
}: {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  getCurrentTimeline: () => SessionTurnTimeline | null;
  generationRef: MutableRefObject<number>;
  requestSignal: AbortSignal;
  setState: SetAppState;
  direction: TurnPageDirection;
}): () => Promise<void> {
  return useCallback(async () => {
    if (!apiPort || !sessionId || !sessionCacheKey) return;
    const targetGeneration = generationRef.current;
    const currentTimeline = getCurrentTimeline();
    const cursor = currentTimeline ? pageCursor(currentTimeline, direction) : null;
    if (
      !currentTimeline
      || currentTimeline.generation !== targetGeneration
      || isLoading(currentTimeline, direction)
      || !pageHasMore(currentTimeline, direction)
      || !cursor
    ) {
      return;
    }
    setState((previous) => {
      const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
      if (
        timeline.generation !== targetGeneration
        || isLoading(timeline, direction)
        || !pageHasMore(timeline, direction)
        || pageCursor(timeline, direction) !== cursor
      ) {
        return previous;
      }
      return {
        ...previous,
        turnTimelinesBySession: writeTurnTimelineCache(
          previous.turnTimelinesBySession,
          sessionCacheKey,
          { ...timeline, ...loadingPatch(direction, true), error: null },
        ),
      };
    });

    try {
      const page = await loadSessionHistory(
        apiPort,
        sessionId,
        { direction, cursor, turns: CURSOR_HISTORY_TURNS },
        workspaceId,
        requestSignal,
      );
      setState((previous) => {
        const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
        if (timeline.generation !== targetGeneration) return previous;
        const epochDecision = decideTurnProjectionEpoch(
          timeline.projectionEpoch,
          page.projection_epoch,
        );
        if (epochDecision === "discard_older") {
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              { ...timeline, ...loadingPatch(direction, false) },
            ),
          };
        }
        if (epochDecision === "refresh_bootstrap") {
          return {
            ...previous,
            sessionHistoryReloadNonce: previous.sessionHistoryReloadNonce + 1,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              { ...timeline, ...loadingPatch(direction, false) },
            ),
            status: "Turn 投影已更新，正在重新加载",
          };
        }
        return {
          ...previous,
          turnTimelinesBySession: writeTurnTimelineCache(
            previous.turnTimelinesBySession,
            sessionCacheKey,
            applyTurnHistoryPage(timeline, page, direction),
          ),
        };
      });
    } catch (error) {
      if (requestSignal.aborted) {
        setState((previous) => {
          const timeline = timelineForScope(
            previous.turnTimelinesBySession,
            sessionCacheKey,
          );
          if (
            timeline.generation !== targetGeneration
            || !isLoading(timeline, direction)
          ) {
            return previous;
          }
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              { ...timeline, ...loadingPatch(direction, false) },
            ),
          };
        });
        return;
      }
      if (error instanceof StaleTurnCursorHttpError) {
        setState((previous) => {
          const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
          if (timeline.generation !== targetGeneration) return previous;
          return {
            ...previous,
            sessionHistoryReloadNonce: previous.sessionHistoryReloadNonce + 1,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              { ...timeline, ...loadingPatch(direction, false) },
            ),
            status: "Turn 历史游标已失效，正在重新校准",
          };
        });
        return;
      }
      if (isTransientNetworkError(error)) {
        setState((previous) => {
          const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
          if (timeline.generation !== targetGeneration) return previous;
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              preserveTimelineAfterTransientNetworkFailure(
                { ...timeline, ...loadingPatch(direction, false) },
                targetGeneration,
              ),
            ),
            status: "历史连接暂时变化，已保留当前内容，可继续重试",
          };
        });
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      setState((previous) => {
        const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
        if (timeline.generation !== targetGeneration) return previous;
        return {
          ...previous,
          turnTimelinesBySession: writeTurnTimelineCache(
            previous.turnTimelinesBySession,
            sessionCacheKey,
            failTurnTimeline(timeline, targetGeneration, message),
          ),
        };
      });
      throw error;
    }
  }, [
    apiPort,
    direction,
    generationRef,
    getCurrentTimeline,
    requestSignal,
    sessionCacheKey,
    sessionId,
    setState,
    workspaceId,
  ]);
}

export function useOlderTurnLoader(props: Omit<Parameters<typeof useDirectionalTurnLoader>[0], "direction">): () => Promise<void> {
  return useDirectionalTurnLoader({ ...props, direction: "before" });
}

export function useNewerTurnLoader(props: Omit<Parameters<typeof useDirectionalTurnLoader>[0], "direction">): () => Promise<void> {
  return useDirectionalTurnLoader({ ...props, direction: "after" });
}

export function useAroundTurnLoader({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  getCurrentTimeline,
  generationRef,
  requestSignal,
  setState,
}: Omit<Parameters<typeof useDirectionalTurnLoader>[0], "direction">):
  (anchorTurnId: string) => Promise<void> {
  return useCallback(async (anchorTurnId: string) => {
    if (!apiPort || !sessionId || !sessionCacheKey || !anchorTurnId) return;
    const targetGeneration = generationRef.current;
    const currentTimeline = getCurrentTimeline();
    if (!currentTimeline || currentTimeline.generation !== targetGeneration) return;
    setState((previous) => {
      const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
      if (timeline.generation !== targetGeneration) return previous;
      return {
        ...previous,
        turnTimelinesBySession: writeTurnTimelineCache(
          previous.turnTimelinesBySession,
          sessionCacheKey,
          {
            ...timeline,
            loadingBefore: true,
            loadingAfter: true,
            loadingOlder: true,
            error: null,
          },
        ),
      };
    });
    try {
      const page = await loadSessionHistory(
        apiPort,
        sessionId,
        {
          direction: "around",
          anchor_turn_id: anchorTurnId,
          before_turns: CURSOR_HISTORY_TURNS,
          after_turns: CURSOR_HISTORY_TURNS,
        },
        workspaceId,
        requestSignal,
      );
      setState((previous) => {
        const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
        if (timeline.generation !== targetGeneration) return previous;
        const epochDecision = decideTurnProjectionEpoch(
          timeline.projectionEpoch,
          page.projection_epoch,
        );
        if (epochDecision === "discard_older") {
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              {
                ...timeline,
                loadingBefore: false,
                loadingAfter: false,
                loadingOlder: false,
              },
            ),
          };
        }
        if (epochDecision === "refresh_bootstrap") {
          return {
            ...previous,
            sessionHistoryReloadNonce: previous.sessionHistoryReloadNonce + 1,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              {
                ...timeline,
                loadingBefore: false,
                loadingAfter: false,
                loadingOlder: false,
              },
            ),
            status: "Turn 投影已更新，正在重新加载",
          };
        }
        return {
          ...previous,
          turnTimelinesBySession: writeTurnTimelineCache(
            previous.turnTimelinesBySession,
            sessionCacheKey,
            applyTurnHistoryPage(timeline, page, "around"),
          ),
        };
      });
    } catch (error) {
      if (requestSignal.aborted) {
        setState((previous) => {
          const timeline = timelineForScope(
            previous.turnTimelinesBySession,
            sessionCacheKey,
          );
          if (timeline.generation !== targetGeneration) return previous;
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              {
                ...timeline,
                loadingBefore: false,
                loadingAfter: false,
                loadingOlder: false,
              },
            ),
          };
        });
        return;
      }
      if (error instanceof StaleTurnCursorHttpError) {
        setState((previous) => {
          const timeline = timelineForScope(
            previous.turnTimelinesBySession,
            sessionCacheKey,
          );
          if (timeline.generation !== targetGeneration) return previous;
          return {
            ...previous,
            sessionHistoryReloadNonce: previous.sessionHistoryReloadNonce + 1,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              {
                ...timeline,
                loadingBefore: false,
                loadingAfter: false,
                loadingOlder: false,
              },
            ),
            status: "Turn 历史游标已失效，正在重新校准",
          };
        });
        return;
      }
      if (error instanceof StaleTurnReferenceHttpError) {
        setState((previous) => {
          const timeline = timelineForScope(
            previous.turnTimelinesBySession,
            sessionCacheKey,
          );
          if (timeline.generation !== targetGeneration) return previous;
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              {
                ...timeline,
                loadingBefore: false,
                loadingAfter: false,
                loadingOlder: false,
              },
            ),
            status: "保存的历史位置已失效，已准备从最新位置加载",
          };
        });
        return;
      }
      if (isTransientNetworkError(error)) {
        setState((previous) => {
          const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
          if (timeline.generation !== targetGeneration) return previous;
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              preserveTimelineAfterTransientNetworkFailure(
                timeline,
                targetGeneration,
              ),
            ),
            status: "历史连接暂时变化，已保留当前内容，可继续重试",
          };
        });
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      setState((previous) => {
        const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
        if (timeline.generation !== targetGeneration) return previous;
        return {
          ...previous,
          turnTimelinesBySession: writeTurnTimelineCache(
            previous.turnTimelinesBySession,
            sessionCacheKey,
            failTurnTimeline(timeline, targetGeneration, message),
          ),
        };
      });
      throw error;
    }
  }, [
    apiPort,
    generationRef,
    getCurrentTimeline,
    requestSignal,
    sessionCacheKey,
    sessionId,
    setState,
    workspaceId,
  ]);
}

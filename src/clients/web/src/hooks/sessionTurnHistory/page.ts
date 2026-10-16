import { useCallback, useRef, type MutableRefObject } from "react";
import { HttpRequestError, isTransientNetworkError } from "../../api/http";
import {
  loadSessionHistory,
  StaleTurnCursorHttpError,
  StaleTurnReferenceHttpError,
} from "../../api/session/sessionTurnHistory";
import {
  applyTurnHistoryPage,
  decideTurnProjectionEpoch,
  writeTurnTimelineCache,
  type SessionTurnTimeline,
} from "../../state/session/turnTimeline";
import type { SetAppState } from "../contentViewLoaderTypes";
import { errorMessage } from "../../utils/errorMessage";
import {
  preserveTimelineAfterTransientNetworkFailure,
  timelineForScope,
  type TurnScopeLoaderProps,
  waitForDelayAborted,
  writeTurnLoadFailure,
  writeTransientNetworkPreserved,
} from "./turnLoadSupport";

type TurnPageDirection = "before" | "after";

const INITIAL_HISTORY_TURNS = 5;
const CURSOR_HISTORY_TURNS = 3;
const INITIAL_HISTORY_RETRY_DELAYS_MS = [100, 250, 500, 1000, 2000] as const;

function pageCursor(
  timeline: SessionTurnTimeline,
  direction: TurnPageDirection,
): string | null {
  return direction === "before"
    ? timeline.beforeCursor
    : timeline.afterCursor;
}

function pageHasMore(
  timeline: SessionTurnTimeline,
  direction: TurnPageDirection,
): boolean {
  return direction === "before" ? timeline.hasBefore : timeline.hasAfter;
}

function loadingPatch(
  direction: TurnPageDirection,
  loading: boolean,
): Partial<SessionTurnTimeline> {
  return direction === "before"
    ? { loadingBefore: loading }
    : { loadingAfter: loading };
}

function isLoading(
  timeline: SessionTurnTimeline,
  direction: TurnPageDirection,
): boolean {
  return direction === "before" ? timeline.loadingBefore : timeline.loadingAfter;
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

export function useInitialTurnLoader({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  generationRef,
  requestSignal,
  setState,
  onMissingTurn,
}: TurnScopeLoaderProps): (latestTurnId?: string) => Promise<void> {
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
          const shouldContinue = await waitForDelayAborted(
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
              { ...timeline, loadingBefore: false },
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
              { ...timeline, loadingBefore: false },
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
              { ...timeline, loadingBefore: false },
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
                error: null,
              },
            ),
            status: "Turn 历史正在提交，已保留当前回合，稍后重试",
          };
        });
        return;
      }
      if (isTransientNetworkError(error)) {
        writeTransientNetworkPreserved(setState, sessionCacheKey, targetGeneration);
        return;
      }
      if (error instanceof HttpRequestError && error.status === 404 && latestTurnId) {
        onMissingTurn([latestTurnId]);
        return;
      }
      const message = errorMessage(error);
      writeTurnLoadFailure(setState, sessionCacheKey, targetGeneration, message);
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
      const message = errorMessage(error);
      writeTurnLoadFailure(setState, sessionCacheKey, targetGeneration, message);
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
  (anchorTurnId: string, signal?: AbortSignal) => Promise<void> {
  const activeRequestRef = useRef<AbortController | null>(null);
  return useCallback(async (anchorTurnId: string, navigationSignal?: AbortSignal) => {
    if (!apiPort || !sessionId || !sessionCacheKey || !anchorTurnId) {
      throw new Error("当前会话尚未就绪，无法定位轮次");
    }
    const targetGeneration = generationRef.current;
    const currentTimeline = getCurrentTimeline();
    if (!currentTimeline?.threadId || currentTimeline.generation !== targetGeneration) {
      throw new Error("当前线程历史尚未就绪，请稍后重试");
    }
    activeRequestRef.current?.abort();
    const controller = new AbortController();
    activeRequestRef.current = controller;
    const signal = AbortSignal.any([requestSignal, controller.signal, ...(navigationSignal ? [navigationSignal] : [])]);
    const isCurrentRequest = () => activeRequestRef.current === controller
      && generationRef.current === targetGeneration
      && getCurrentTimeline()?.threadId === currentTimeline.threadId;
    const releaseLoading = () => {
      setState((previous) => {
        const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
        // 离开 owner 后仍可清原 generation；同代后继请求的标志由它自己持有。
        if (activeRequestRef.current !== controller
          || timeline.generation !== targetGeneration
          || timeline.threadId !== currentTimeline.threadId) return previous;
        return {
          ...previous,
          turnTimelinesBySession: writeTurnTimelineCache(
            previous.turnTimelinesBySession,
            sessionCacheKey,
            { ...timeline, loadingBefore: false, loadingAfter: false },
          ),
        };
      });
    };
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
        signal,
        currentTimeline.threadId,
      );
      signal.throwIfAborted();
      if (!isCurrentRequest()) throw new Error("轮次定位请求的会话线程已切换");
      if (page.thread_id !== currentTimeline.threadId) throw new Error("指定轮次历史的 thread owner 与请求不一致");
      if (![...page.items, ...(page.summaries ?? [])].some((turn) => turn.turn_id === anchorTurnId)) {
        throw new Error("当前会话线程中未找到指定轮次：" + anchorTurnId);
      }
      const epochDecision = decideTurnProjectionEpoch(getCurrentTimeline()?.projectionEpoch ?? null, page.projection_epoch);
      if (epochDecision !== "apply") {
        throw new Error("轮次历史版本已变化，请刷新历史后重试指定轮次");
      }
      setState((previous) => {
        if (signal.aborted || !isCurrentRequest()) return previous;
        const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
        if (timeline.generation !== targetGeneration) return previous;
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
      if (signal.aborted) {
        releaseLoading();
        throw error;
      }
      if (!isCurrentRequest()) throw error;
      if (error instanceof HttpRequestError && error.status === 404) {
        // 指定目标不存在只属于本次导航；保留可用历史与它的事件流。
        releaseLoading();
        throw error;
      }
      if (isTransientNetworkError(error)) {
        writeTransientNetworkPreserved(setState, sessionCacheKey, targetGeneration);
        throw error;
      }
      const message = errorMessage(error);
      writeTurnLoadFailure(setState, sessionCacheKey, targetGeneration, message);
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

import { useCallback, type MutableRefObject } from "react";
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
        { direction, cursor },
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

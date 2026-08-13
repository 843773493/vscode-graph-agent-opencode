import { useCallback, type MutableRefObject } from "react";
import {
  listSessionTurns,
  StaleTurnCursorHttpError,
} from "../../api/sessionTurnHistory";
import {
  applyTurnPage,
  createSessionTurnTimeline,
  decideTurnProjectionEpoch,
  failTurnTimeline,
  writeTurnTimelineCache,
  type SessionTurnTimeline,
} from "../../state/session/turnTimeline";
import type { SetAppState } from "../contentViewLoaderTypes";

function timelineForScope(
  timelines: Map<string, SessionTurnTimeline>,
  scopeKey: string,
): SessionTurnTimeline {
  return timelines.get(scopeKey) ?? createSessionTurnTimeline(scopeKey);
}

export function useOlderTurnLoader({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  generationRef,
  requestSignal,
  setState,
}: {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  generationRef: MutableRefObject<number>;
  requestSignal: AbortSignal;
  setState: SetAppState;
}): () => Promise<void> {
  return useCallback(async () => {
    if (!apiPort || !sessionId || !sessionCacheKey) return;
    const targetGeneration = generationRef.current;
    let cursor: string | null = null;
    setState((previous) => {
      const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
      if (
        timeline.generation !== targetGeneration
        || timeline.loadingOlder
        || !timeline.hasMore
        || !timeline.olderCursor
      ) {
        return previous;
      }
      cursor = timeline.olderCursor;
      return {
        ...previous,
        turnTimelinesBySession: writeTurnTimelineCache(
          previous.turnTimelinesBySession,
          sessionCacheKey,
          { ...timeline, loadingOlder: true, error: null },
        ),
      };
    });
    if (!cursor) return;

    try {
      const page = await listSessionTurns(apiPort, sessionId, workspaceId, {
        cursor,
        limit: 20,
        signal: requestSignal,
      });
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
              { ...timeline, loadingOlder: false },
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
              { ...timeline, loadingOlder: false },
            ),
            status: "Turn 投影已更新，正在重新加载",
          };
        }
        return {
          ...previous,
          turnTimelinesBySession: writeTurnTimelineCache(
            previous.turnTimelinesBySession,
            sessionCacheKey,
            applyTurnPage(timeline, page),
          ),
        };
      });
    } catch (error) {
      if (requestSignal.aborted) return;
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
              { ...timeline, loadingOlder: false },
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
    generationRef,
    requestSignal,
    sessionCacheKey,
    sessionId,
    setState,
    workspaceId,
  ]);
}

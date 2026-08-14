import {
  startTransition,
  useCallback,
  useRef,
  type MutableRefObject,
} from "react";
import { HttpRequestError } from "../../api/http";
import { getSessionTurnDetails } from "../../api/sessionTurnHistory";
import {
  applyTurnDetails,
  createSessionTurnTimeline,
  decideTurnProjectionEpoch,
  failTurnTimeline,
  markTurnsLoading,
  writeTurnTimelineCache,
  type SessionTurnTimeline,
} from "../../state/session/turnTimeline";
import type { TurnDetailBatchRequest } from "../../types/backend";
import type { SetAppState } from "../contentViewLoaderTypes";

function timelineForScope(
  timelines: Map<string, SessionTurnTimeline>,
  scopeKey: string,
): SessionTurnTimeline {
  return timelines.get(scopeKey) ?? createSessionTurnTimeline(scopeKey);
}

function detailRequestIds(turnIds: string[]): TurnDetailBatchRequest["turn_ids"] {
  const uniqueIds = [...new Set(turnIds.filter(Boolean))];
  if (uniqueIds.length < 1 || uniqueIds.length > 4) {
    throw new Error(`Turn 详情请求数量必须在 1 到 4 之间，实际为 ${uniqueIds.length}`);
  }
  return uniqueIds as TurnDetailBatchRequest["turn_ids"];
}

export function useTurnDetailLoader({
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
  onMissingTurn: () => void;
}): (
  turnIds: string[],
  requestIdentity?: string | null,
  refreshAfterInFlight?: boolean,
) => Promise<void> {
  const inFlightByTurnId = useRef(new Map<string, {
    requestIdentity: string | null;
    request: Promise<void>;
  }>());
  const invalidationVersionByTurnId = useRef(new Map<string, number>());
  const fulfilledInvalidationByTurnId = useRef(new Map<string, number>());
  const invalidationLoopByTurnId = useRef(new Map<string, Promise<void>>());
  const inFlightScopeSignal = useRef(requestSignal);
  if (inFlightScopeSignal.current !== requestSignal) {
    inFlightScopeSignal.current = requestSignal;
    inFlightByTurnId.current.clear();
    invalidationVersionByTurnId.current.clear();
    fulfilledInvalidationByTurnId.current.clear();
    invalidationLoopByTurnId.current.clear();
  }
  const requestNewDetails = useCallback(async (
    requestIds: TurnDetailBatchRequest["turn_ids"],
  ) => {
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
          markTurnsLoading(timeline, requestIds),
        ),
      };
    });

    try {
      const batch = await getSessionTurnDetails(
        apiPort,
        sessionId,
        requestIds,
        workspaceId,
        requestSignal,
      );
      startTransition(() => {
        setState((previous) => {
          const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
          if (timeline.generation !== targetGeneration) return previous;
          const epochDecision = decideTurnProjectionEpoch(
            timeline.projectionEpoch,
            batch.projection_epoch,
          );
          if (epochDecision === "discard_older") return previous;
          if (epochDecision === "refresh_bootstrap") {
            return {
              ...previous,
              sessionHistoryReloadNonce: previous.sessionHistoryReloadNonce + 1,
              status: "Turn 投影已更新，正在重新加载",
            };
          }
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              applyTurnDetails(timeline, batch),
            ),
          };
        });
      });
    } catch (error) {
      if (requestSignal.aborted) return;
      if (error instanceof HttpRequestError && error.status === 404) {
        onMissingTurn();
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
  return useCallback(async (
    turnIds: string[],
    requestIdentity: string | null = null,
    refreshAfterInFlight: boolean = false,
  ) => {
    if (!apiPort || !sessionId || !sessionCacheKey || turnIds.length === 0) return;
    const requestIds = detailRequestIds(turnIds);
    if (refreshAfterInFlight) {
      const refreshLoops = requestIds.map((turnId) => {
        invalidationVersionByTurnId.current.set(
          turnId,
          (invalidationVersionByTurnId.current.get(turnId) ?? 0) + 1,
        );
        const existingLoop = invalidationLoopByTurnId.current.get(turnId);
        if (existingLoop) return existingLoop;

        let refreshLoop: Promise<void>;
        refreshLoop = (async () => {
          const initialRequest = inFlightByTurnId.current.get(turnId)?.request;
          if (initialRequest) {
            await Promise.allSettled([initialRequest]);
          }
          while (!requestSignal.aborted) {
            const desiredVersion =
              invalidationVersionByTurnId.current.get(turnId) ?? 0;
            const fulfilledVersion =
              fulfilledInvalidationByTurnId.current.get(turnId) ?? 0;
            if (fulfilledVersion >= desiredVersion) return;

            const request = requestNewDetails(
              [turnId] as TurnDetailBatchRequest["turn_ids"],
            );
            inFlightByTurnId.current.set(turnId, {
              requestIdentity: null,
              request,
            });
            try {
              await request;
              fulfilledInvalidationByTurnId.current.set(
                turnId,
                desiredVersion,
              );
            } finally {
              if (inFlightByTurnId.current.get(turnId)?.request === request) {
                inFlightByTurnId.current.delete(turnId);
              }
            }
          }
        })().finally(() => {
          if (invalidationLoopByTurnId.current.get(turnId) === refreshLoop) {
            invalidationLoopByTurnId.current.delete(turnId);
          }
        });
        invalidationLoopByTurnId.current.set(turnId, refreshLoop);
        return refreshLoop;
      });
      await Promise.all([...new Set(refreshLoops)]);
      return;
    }

    const pendingRequests: Promise<void>[] = [];
    const newIds: string[] = [];
    for (const turnId of requestIds) {
      const invalidationLoop = invalidationLoopByTurnId.current.get(turnId);
      if (invalidationLoop) {
        pendingRequests.push(invalidationLoop);
        continue;
      }
      const pending = inFlightByTurnId.current.get(turnId);
      if (!pending || (
        requestIdentity !== null
        && pending.requestIdentity !== requestIdentity
      )) {
        newIds.push(turnId);
      } else {
        pendingRequests.push(pending.request);
      }
    }
    if (newIds.length > 0) {
      let request: Promise<void>;
      request = requestNewDetails(
        newIds as TurnDetailBatchRequest["turn_ids"],
      ).finally(() => {
        for (const turnId of newIds) {
          if (inFlightByTurnId.current.get(turnId)?.request === request) {
            inFlightByTurnId.current.delete(turnId);
          }
        }
      });
      for (const turnId of newIds) {
        inFlightByTurnId.current.set(turnId, {
          requestIdentity,
          request,
        });
      }
      pendingRequests.push(request);
    }
    await Promise.all([...new Set(pendingRequests)]);
  }, [
    apiPort,
    requestNewDetails,
    requestSignal,
    sessionCacheKey,
    sessionId,
  ]);
}

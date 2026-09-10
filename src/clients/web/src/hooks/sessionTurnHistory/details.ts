import {
  startTransition,
  useCallback,
  useRef,
  type MutableRefObject,
} from "react";
import { HttpRequestError, isTransientNetworkError } from "../../api/http";
import {
  loadSessionHistory,
  StaleTurnCursorHttpError,
  StaleTurnReferenceHttpError,
  type TurnHistoryInclude,
} from "../../api/sessionTurnHistory";
import {
  applyTurnDetails,
  createSessionTurnTimeline,
  decideTurnProjectionEpoch,
  failTurnTimeline,
  markTurnsLoading,
  upsertTurns,
  writeTurnTimelineCache,
  type SessionTurnTimeline,
} from "../../state/session/turnTimeline";
import type { TurnDetailBatchRequest } from "../../types/backend";
import type { SetAppState } from "../contentViewLoaderTypes";

const TURN_DETAIL_COMMIT_RETRY_DELAYS_MS = [100, 250, 500, 1000] as const;

function isTurnProjectionCommitConflict(error: unknown): boolean {
  return error instanceof StaleTurnCursorHttpError
    || (error instanceof HttpRequestError && error.status === 409);
}

async function waitForTurnCommit(
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
  onMissingTurn: (turnIds: string[]) => void;
}): (
  turnIds: string[],
  requestIdentity?: string | null,
  refreshAfterInFlight?: boolean,
  include?: TurnHistoryInclude[],
  toolCallIds?: string[],
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
    include?: TurnHistoryInclude[],
    toolCallIds?: string[],
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
      let page: Awaited<ReturnType<typeof loadSessionHistory>> | null = null;
      for (
        let attempt = 0;
        attempt <= TURN_DETAIL_COMMIT_RETRY_DELAYS_MS.length;
        attempt += 1
      ) {
        try {
          page = await loadSessionHistory(
            apiPort,
            sessionId,
            {
              direction: "around",
              turn_ids: requestIds,
              ...(include ? { include } : {}),
              ...(toolCallIds ? { tool_call_ids: toolCallIds } : {}),
            },
            workspaceId,
            requestSignal,
          );
          break;
        } catch (error) {
          // stale_turn_reference 表示请求携带的旧 Turn 已不属于当前上下文，
          // 不是投影写锁。继续重试同一个旧 ID 只会制造持续的 409 风暴。
          if (error instanceof StaleTurnReferenceHttpError) {
            throw error;
          }
          if (
            (!(error instanceof HttpRequestError) || error.status !== 404)
            && !isTurnProjectionCommitConflict(error)
          ) {
            throw error;
          }
          if (attempt >= TURN_DETAIL_COMMIT_RETRY_DELAYS_MS.length) {
            throw error;
          }
          const shouldContinue = await waitForTurnCommit(
            TURN_DETAIL_COMMIT_RETRY_DELAYS_MS[attempt],
            requestSignal,
          );
          if (!shouldContinue) return;
        }
      }
      if (page === null) {
        throw new Error("Turn 详情请求未返回结果");
      }
      startTransition(() => {
        setState((previous) => {
          const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
          if (timeline.generation !== targetGeneration) return previous;
          const epochDecision = decideTurnProjectionEpoch(
            timeline.projectionEpoch,
            page.projection_epoch,
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
              applyTurnDetails(
                upsertTurns(timeline, page.summaries ?? []),
                {
                  items: page.items,
                  projection_epoch: page.projection_epoch,
                },
              ),
            ),
          };
        });
      });
    } catch (error) {
      if (requestSignal.aborted) return;
      if (error instanceof StaleTurnReferenceHttpError) {
        // 旧 Turn 由 bootstrap 重新校准；必须登记为失效引用，避免
        // SSE/详情回放再次携带同一批旧 ID，持续制造 409 风暴。
        // refreshTurnHistory 只移除时间线中的旧 Turn，不会清空会话树或实时 pending conversation。
        onMissingTurn(requestIds);
        return;
      }
      if (isTurnProjectionCommitConflict(error)) {
        setState((previous) => ({
          ...previous,
          status: "Turn 历史正在提交，已保留当前回合，稍后重试",
        }));
        throw error;
      }
      if (error instanceof HttpRequestError && error.status === 404) {
        onMissingTurn(requestIds);
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
              {
                ...timeline,
                loadingDetailIds: timeline.loadingDetailIds.filter(
                  (turnId) => !requestIds.includes(turnId),
                ),
                error: null,
              },
            ),
            status: "历史连接暂时变化，已保留当前回合，可继续重试",
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
    include?: TurnHistoryInclude[],
    toolCallIds?: string[],
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
              include,
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
        include,
        toolCallIds,
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

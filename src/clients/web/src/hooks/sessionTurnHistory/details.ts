import {
  startTransition,
  useCallback,
  useRef,
  type MutableRefObject,
} from "react";
import { HttpRequestError } from "../../api/http";
import { getSessionMessageStreamSnapshot } from "../../api/sessionMessageStream";
import {
  loadSessionHistory,
  StaleTurnReferenceHttpError,
  type TurnHistoryInclude,
} from "../../api/sessionTurnHistory";
import { cloneMaps } from "../../state/appStateMaps";
import {
  applyMessageStreamEvent,
  type MessageStreamEvent,
} from "../../state/messageStream";
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

const TURN_DETAIL_COMMIT_RETRY_DELAYS_MS = [100, 250, 500, 1000] as const;

async function hydrateMessageStreamSnapshots(
  apiPort: number,
  sessionId: string,
  workspaceId: string | null,
  turnIds: string[],
  signal: AbortSignal,
  setState: SetAppState,
): Promise<void> {
  const snapshots = await Promise.all(turnIds.map(async (turnId) => {
    try {
      return await getSessionMessageStreamSnapshot(
        apiPort,
        sessionId,
        turnId,
        { workspaceId, signal },
      );
    } catch (error) {
      // 历史上没有 message.v1 的 Turn 没有 snapshot；这是明确的存量归档路径，
      // 不能把它误报成详情加载失败。
      if (error instanceof HttpRequestError && error.status === 404) {
        return null;
      }
      throw error;
    }
  }));

  if (signal.aborted) return;
  setState((previous) => {
    const next = cloneMaps(previous);
    const messageStreams = next.messageStreamsByTurnStream ?? new Map();
    next.messageStreamsByTurnStream = messageStreams;
    for (const snapshot of snapshots) {
      if (!snapshot) continue;
      const event: MessageStreamEvent = {
        event_id: `snapshot:${snapshot.turn_id}:${snapshot.snapshot_seq}`,
        session_id: snapshot.session_id,
        turn_id: snapshot.turn_id,
        turn_stream_id: snapshot.turn_stream_id,
        event_seq: snapshot.snapshot_seq,
        type: "stream.snapshot",
        payload: snapshot as unknown as Record<string, unknown>,
      };
      const currentEntry = [...messageStreams.entries()].find(
        ([, stream]) =>
          stream.sessionId === snapshot.session_id
          && stream.turnId === snapshot.turn_id,
      );
      const current = currentEntry?.[1] ?? null;
      if (current && current.lastEventSeq > snapshot.snapshot_seq) {
        continue;
      }
      const updated = applyMessageStreamEvent(current, event);
      for (const [key, stream] of messageStreams.entries()) {
        if (
          key !== snapshot.turn_stream_id
          && stream.sessionId === snapshot.session_id
          && stream.turnId === snapshot.turn_id
        ) {
          messageStreams.delete(key);
        }
      }
      messageStreams.set(snapshot.turn_stream_id, updated);
    }
    return next;
  });
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
            },
            workspaceId,
            requestSignal,
          );
          break;
        } catch (error) {
          if (
            !(error instanceof HttpRequestError)
            || error.status !== 404
            || attempt >= TURN_DETAIL_COMMIT_RETRY_DELAYS_MS.length
          ) {
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
            applyTurnDetails(timeline, {
              items: page.items,
              projection_epoch: page.projection_epoch,
            }),
            ),
          };
        });
      });
      // Turn 详情负责历史入口；一旦该 Turn 存在 message.v1 持久化流，
      // snapshot 必须覆盖旧 response_parts，保证刷新/重新选中后仍使用同一语义。
      void hydrateMessageStreamSnapshots(
        apiPort,
        sessionId,
        workspaceId,
        requestIds,
        requestSignal,
        setState,
      ).catch((error: unknown) => {
        if (requestSignal.aborted) return;
        const message = error instanceof Error ? error.message : String(error);
        setState((previous) => ({
          ...previous,
          status: `恢复 Turn 消息流 snapshot 失败: ${message}`,
        }));
      });
    } catch (error) {
      if (requestSignal.aborted) return;
      if (error instanceof StaleTurnReferenceHttpError) {
        onMissingTurn(error.detail.turn_ids);
        return;
      }
      if (error instanceof HttpRequestError && error.status === 404) {
        onMissingTurn(requestIds);
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

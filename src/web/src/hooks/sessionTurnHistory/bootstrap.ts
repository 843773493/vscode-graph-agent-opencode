import { useEffect, useRef, type MutableRefObject } from "react";
import { getSessionTurnBootstrap } from "../../api/sessionTurnHistory";
import { listPendingRequests } from "../../pendingRequestsApi";
import { cloneMaps } from "../../state/appStateMaps";
import { replaceSessionMetadata } from "../../state/session/sessions";
import {
  syncActiveJobConversation,
  writePendingSnapshot,
} from "../../state/conversations";
import {
  applyTurnBootstrap,
  beginTurnBootstrap,
  createSessionTurnTimeline,
  failTurnTimeline,
  writeTurnTimelineCache,
  type SessionTurnTimeline,
} from "../../state/session/turnTimeline";
import type { SetAppState } from "../contentViewLoaderTypes";

const PARTIAL_BOOTSTRAP_POLL_BASE_DELAY_MS = 250;
const PARTIAL_BOOTSTRAP_POLL_MAX_DELAY_MS = 2_000;

export function partialBootstrapPollDelay(attempt: number): number {
  if (!Number.isInteger(attempt) || attempt < 0) {
    throw new Error(`Turn bootstrap 轮询次数必须是非负整数，实际为 ${attempt}`);
  }
  return Math.min(
    PARTIAL_BOOTSTRAP_POLL_BASE_DELAY_MS * (2 ** Math.min(attempt, 3)),
    PARTIAL_BOOTSTRAP_POLL_MAX_DELAY_MS,
  );
}

function timelineForScope(
  timelines: Map<string, SessionTurnTimeline>,
  scopeKey: string,
): SessionTurnTimeline {
  return timelines.get(scopeKey) ?? createSessionTurnTimeline(scopeKey);
}

export function useTurnBootstrap({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  reloadNonce,
  manualReloadNonce,
  generationRef,
  setState,
  loadTurnDetails,
}: {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  reloadNonce: number;
  manualReloadNonce: number;
  generationRef: MutableRefObject<number>;
  setState: SetAppState;
  loadTurnDetails: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
  ) => Promise<void>;
}): void {
  const bootstrapAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!apiPort || !sessionId || !sessionCacheKey) {
      bootstrapAbortRef.current?.abort();
      return;
    }
    bootstrapAbortRef.current?.abort();
    const controller = new AbortController();
    bootstrapAbortRef.current = controller;
    const targetGeneration = generationRef.current + 1;
    generationRef.current = targetGeneration;
    let pollTimer: ReturnType<typeof globalThis.setTimeout> | null = null;
    let latestDetailIdentity: string | null = null;
    let pendingSnapshotIdentity: string | null = null;

    setState((previous) => {
      const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
      return {
        ...previous,
        turnTimelinesBySession: writeTurnTimelineCache(
          previous.turnTimelinesBySession,
          sessionCacheKey,
          beginTurnBootstrap(timeline, targetGeneration),
        ),
        status: timeline.orderedTurnIds.length > 0
          ? "已显示 Turn 缓存，正在校准最新状态"
          : "正在加载最新 Turn",
      };
    });

    const requestBootstrap = async (pollAttempt: number): Promise<void> => {
      try {
        const bootstrap = await getSessionTurnBootstrap(
          apiPort,
          sessionId,
          workspaceId,
          controller.signal,
        );
        if (controller.signal.aborted || generationRef.current !== targetGeneration) return;
        const projectionState = bootstrap.projection_state ?? "ready";
        const activeJobCount = bootstrap.active_job_count
          ?? bootstrap.active_jobs?.length
          ?? 0;
        setState((previous) => {
          if (
            previous.currentSession?.session_id !== sessionId
            || (workspaceId && previous.currentSessionWorkspaceId !== workspaceId)
          ) {
            return previous;
          }
          let next = replaceSessionMetadata(previous, bootstrap.session, workspaceId);
          const timeline = timelineForScope(next.turnTimelinesBySession, sessionCacheKey);
          next.turnTimelinesBySession = writeTurnTimelineCache(
            next.turnTimelinesBySession,
            sessionCacheKey,
            applyTurnBootstrap(timeline, targetGeneration, bootstrap),
          );
          const activeJobId = bootstrap.active_job_id ?? null;
          if (activeJobId) {
            next.activeJobIdsBySession.set(sessionCacheKey, activeJobId);
            syncActiveJobConversation(
              next.pendingConversations,
              sessionId,
              activeJobId,
              sessionCacheKey,
            );
          } else {
            next.activeJobIdsBySession.delete(sessionCacheKey);
            if (activeJobCount === 0) {
              writePendingSnapshot(
                next.pendingConversations,
                next.activeJobIdsBySession,
                {
                  session_id: sessionId,
                  active_job_id: null,
                  requests: [],
                },
                sessionCacheKey,
              );
            } else {
              syncActiveJobConversation(
                next.pendingConversations,
                sessionId,
                null,
                sessionCacheKey,
              );
            }
          }
          next.status = projectionState === "partial"
            ? "最新 Turn 已加载，旧历史正在迁移"
            : "最新 Turn 已加载";
          return next;
        });
        if (activeJobCount > 0) {
          const nextPendingIdentity = [
            bootstrap.active_job_id ?? "none",
            activeJobCount,
            ...(bootstrap.active_jobs ?? []).map(
              (job) => `${job.job_id}:${job.updated_at}`,
            ),
          ].join(":");
          if (nextPendingIdentity !== pendingSnapshotIdentity) {
            pendingSnapshotIdentity = nextPendingIdentity;
            void listPendingRequests(
              apiPort,
              sessionId,
              workspaceId,
              controller.signal,
            ).then((snapshot) => {
              if (
                controller.signal.aborted
                || generationRef.current !== targetGeneration
              ) {
                return;
              }
              setState((previous) => {
                if (
                  previous.currentSession?.session_id !== sessionId
                  || (
                    workspaceId
                    && previous.currentSessionWorkspaceId !== workspaceId
                  )
                ) {
                  return previous;
                }
                const next = cloneMaps(previous);
                writePendingSnapshot(
                  next.pendingConversations,
                  next.activeJobIdsBySession,
                  snapshot,
                  sessionCacheKey,
                );
                return next;
              });
            }).catch((error: unknown) => {
              if (controller.signal.aborted) return;
              const message = error instanceof Error ? error.message : String(error);
              setState((previous) => ({
                ...previous,
                status: `加载待处理消息失败: ${message}`,
              }));
            });
          }
        }
        if (bootstrap.latest_turn) {
          const detailIdentity = [
            bootstrap.projection_epoch,
            bootstrap.latest_turn.turn_id,
            bootstrap.latest_turn.revision,
          ].join(":");
          if (detailIdentity !== latestDetailIdentity) {
            latestDetailIdentity = detailIdentity;
            void loadTurnDetails(
              [bootstrap.latest_turn.turn_id],
              detailIdentity,
            ).catch(() => {
              // 详情错误已写入 Turn timeline，调用方不需要制造第二条全局错误。
            });
          }
        }
        if (projectionState === "partial") {
          pollTimer = globalThis.setTimeout(() => {
            pollTimer = null;
            void requestBootstrap(pollAttempt + 1);
          }, partialBootstrapPollDelay(pollAttempt));
        }
      } catch (error: unknown) {
        if (controller.signal.aborted || generationRef.current !== targetGeneration) return;
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
            status: `加载 Turn 历史失败: ${message}`,
          };
        });
      }
    };

    void requestBootstrap(0);
    return () => {
      if (pollTimer !== null) globalThis.clearTimeout(pollTimer);
      controller.abort();
    };
  }, [
    apiPort,
    generationRef,
    loadTurnDetails,
    manualReloadNonce,
    reloadNonce,
    sessionCacheKey,
    sessionId,
    setState,
    workspaceId,
  ]);
}

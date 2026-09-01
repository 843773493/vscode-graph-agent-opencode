import { useEffect, useRef, type MutableRefObject } from "react";
import { isTransientNetworkError } from "../../api/http";
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
const TRANSIENT_BOOTSTRAP_RETRY_LIMIT = 5;

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
  invalidatedTurnIdsRef,
  setState,
  loadInitialTurns,
}: {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  reloadNonce: number;
  manualReloadNonce: number;
  generationRef: MutableRefObject<number>;
  invalidatedTurnIdsRef: MutableRefObject<Set<string>>;
  setState: SetAppState;
  loadInitialTurns: (latestTurnId?: string) => Promise<void>;
}): void {
  const bootstrapAbortRef = useRef<AbortController | null>(null);
  const lastProjectionEpochRef = useRef<number | null>(null);
  const lastScopeKeyRef = useRef<string | null>(null);
  const visitedScopeKeysRef = useRef(new Set<string>());
  const lastReloadNonceByScopeRef = useRef(new Map<string, number>());
  const lastManualReloadNonceByScopeRef = useRef(new Map<string, number>());

  useEffect(() => {
    if (!apiPort || !sessionId || !sessionCacheKey) {
      bootstrapAbortRef.current?.abort();
      return;
    }
    bootstrapAbortRef.current?.abort();
    const controller = new AbortController();
    bootstrapAbortRef.current = controller;
    const isNewScope = lastScopeKeyRef.current !== sessionCacheKey;
    const hasVisitedScope = visitedScopeKeysRef.current.has(sessionCacheKey);
    const previousReloadNonce = lastReloadNonceByScopeRef.current.get(sessionCacheKey);
    const previousManualReloadNonce = lastManualReloadNonceByScopeRef.current.get(
      sessionCacheKey,
    );
    const shouldLoadInitialHistory = !hasVisitedScope
      || (
        previousReloadNonce !== undefined
        && previousReloadNonce !== reloadNonce
      )
      || (
        previousManualReloadNonce !== undefined
        && previousManualReloadNonce !== manualReloadNonce
      );
    lastReloadNonceByScopeRef.current.set(sessionCacheKey, reloadNonce);
    lastManualReloadNonceByScopeRef.current.set(sessionCacheKey, manualReloadNonce);
    if (isNewScope) {
      invalidatedTurnIdsRef.current.clear();
      lastProjectionEpochRef.current = null;
      lastScopeKeyRef.current = sessionCacheKey;
    }
    visitedScopeKeysRef.current.add(sessionCacheKey);
    const targetGeneration = generationRef.current + 1;
    generationRef.current = targetGeneration;
    let pollTimer: ReturnType<typeof globalThis.setTimeout> | null = null;
    let initialHistoryIdentity: string | null = null;
    let pendingSnapshotIdentity: string | null = null;

    setState((previous) => {
      // 显式刷新/重载必须丢弃旧的上下文窗口。旧窗口中的 Turn 可能已经
      // 不属于当前 context view，继续拿它们请求详情会被后端正确拒绝为 409。
      const timeline = shouldLoadInitialHistory
        ? createSessionTurnTimeline(sessionCacheKey)
        : isNewScope && !hasVisitedScope
          ? createSessionTurnTimeline(sessionCacheKey)
          : timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
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
        if (
          lastProjectionEpochRef.current !== null
          && lastProjectionEpochRef.current !== bootstrap.projection_epoch
        ) {
          invalidatedTurnIdsRef.current.clear();
        }
        lastProjectionEpochRef.current = bootstrap.projection_epoch;
        const activeJobCount = bootstrap.active_job_count
          ?? bootstrap.active_jobs?.length
          ?? 0;
        const activeJobId = bootstrap.active_job_id ?? null;
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
                // replay 新 Job 启动后可能已经离开 queued requests 列表，
                // 但 bootstrap 仍提供 active_job_id。保留这段时间的乐观
                // replay 会话，避免回退提示在首个 SSE 到达前被清掉。
                const snapshotWithActiveJob = snapshot.active_job_id
                  || !activeJobId
                  ? snapshot
                  : { ...snapshot, active_job_id: activeJobId };
                writePendingSnapshot(
                  next.pendingConversations,
                  next.activeJobIdsBySession,
                  snapshotWithActiveJob,
                  sessionCacheKey,
                );
                return next;
              });
            }).catch((error: unknown) => {
              if (controller.signal.aborted) return;
              const message = isTransientNetworkError(error)
                ? "本地服务连接暂时变化，已保留当前队列并自动重试"
                : error instanceof Error ? error.message : String(error);
              setState((previous) => ({
                ...previous,
                status: `加载待处理消息失败: ${message}`,
              }));
            });
          }
        }
        if (
          shouldLoadInitialHistory
          && bootstrap.latest_turn
          && !invalidatedTurnIdsRef.current.has(bootstrap.latest_turn.turn_id)
        ) {
          const initialHistoryRequestIdentity = [
            bootstrap.projection_epoch,
            bootstrap.latest_turn.turn_id,
            bootstrap.latest_turn.revision,
          ].join(":");
          if (initialHistoryRequestIdentity !== initialHistoryIdentity) {
            initialHistoryIdentity = initialHistoryRequestIdentity;
            void loadInitialTurns(bootstrap.latest_turn.turn_id).catch((error: unknown) => {
              if (controller.signal.aborted || generationRef.current !== targetGeneration) {
                return;
              }
              const message = isTransientNetworkError(error)
                ? "本地服务连接暂时变化，历史内容已保留并自动重试"
                : error instanceof Error ? error.message : String(error);
              setState((previous) => ({
                ...previous,
                status: `加载 Turn 历史失败: ${message}`,
              }));
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
        if (isTransientNetworkError(error) && pollAttempt < TRANSIENT_BOOTSTRAP_RETRY_LIMIT) {
          pollTimer = globalThis.setTimeout(() => {
            pollTimer = null;
            void requestBootstrap(pollAttempt + 1);
          }, partialBootstrapPollDelay(pollAttempt));
          setState((previous) => ({
            ...previous,
            status: "会话连接暂时变化，正在有限重试并保留当前内容",
          }));
          return;
        }
        const message = error instanceof Error ? error.message : String(error);
        setState((previous) => {
          const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
          if (timeline.generation !== targetGeneration) return previous;
          const hasVisibleContent = timeline.orderedTurnIds.length > 0;
          return {
            ...previous,
            turnTimelinesBySession: writeTurnTimelineCache(
              previous.turnTimelinesBySession,
              sessionCacheKey,
              hasVisibleContent
                ? {
                    ...timeline,
                    loadingBefore: false,
                    loadingAfter: false,
                    loadingOlder: false,
                    error: null,
                  }
                : failTurnTimeline(
                    timeline,
                    targetGeneration,
                    isTransientNetworkError(error)
                      ? "会话服务暂时不可用，请稍后重试"
                      : message,
                  ),
            ),
            status: isTransientNetworkError(error)
              ? "会话连接暂时不可用，已保留当前内容，请稍后重试"
              : `加载 Turn 历史失败: ${message}`,
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
    invalidatedTurnIdsRef,
    loadInitialTurns,
    manualReloadNonce,
    reloadNonce,
    sessionCacheKey,
    sessionId,
    setState,
    workspaceId,
  ]);
}

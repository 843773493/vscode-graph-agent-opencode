import {
  useCallback,
  useEffect,
  useRef,
} from "react";
import {
  SessionStreamIdleTimeoutError,
  streamSessionEvents,
  TraceCursorGoneError,
} from "../../api/stream/sessionTraceStream";
import { isTransientNetworkError } from "../../api/http";
import { SSE_IDLE_TIMEOUT_MS } from "../../sse/sseIdleTimeout";
import { isJobTerminalTraceType } from "../../state/traceEvents";
import type { SessionStreamEvent } from "../../types/backend";
import {
  ACTIVE_JOB_RECONCILE_INTERVAL_MS,
  ACTIVE_JOB_STALE_PROBE_INTERVAL_MS,
  ACTIVE_JOB_TRACE_STALE_MS,
  SESSION_STREAM_MAX_RECONNECT_ATTEMPTS,
  WORKSPACE_SESSION_FALLBACK_REFRESH_MS,
  sessionStreamReconnectDelay,
} from "./sessionEventStreamPolicy";
import { waitForReconnect } from "./waitForReconnect";
import { reconcileActiveJob } from "./sessionJobReconciliation";
import {
  flushSessionStreamEventBatch,
} from "./batchUpdates";
import {
  refreshWorkspaceSessionList,
} from "./sessionRefresh";
import type { SetAppState } from "../contentViewLoaderTypes";
import { errorMessage } from "../../utils/errorMessage";

export function useSessionEventStream({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  activeJobId,
  timelineReady,
  initialEventCursor,
  refreshTurnHistory,
  loadTerminalTurn,
  setState,
}: {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  activeJobId: string | null;
  timelineReady: boolean;
  initialEventCursor: string | null;
  refreshTurnHistory: () => void;
  loadTerminalTurn: (turnId: string) => Promise<void>;
  setState: SetAppState;
}) {
  const streamAbortRef = useRef<AbortController | null>(null);
  const lastEventCursorRef = useRef<string | null>(null);
  const lastBusinessEventAtRef = useRef<number>(Date.now());
  const lastStaleProbeAtRef = useRef<number>(0);
  const routeRevisionRef = useRef<string | null>(null);
  // 游标失效恢复的有界计数。410 恢复必须触发 Turn bootstrap 重载，而重载会把
  // 本 effect 重建：计数若只放局部变量，每次重建都会归零，上限永远不可达，
  // 「上游持续 410」就会退化成无上限的流重建风暴。计数按会话作用域保存在
  // ref 里，只在切换会话或连接真正建立（收到任意活动，含心跳注释）时归零。
  // 绝不能因为 bootstrap 交回了一个「新」游标就归零：若服务端持续轮换游标
  // 而每个游标都立即失效，那种归零会把上限重新变成不可达。
  const cursorGoneRecoveryRef = useRef<{ scopeKey: string; count: number }>({
    scopeKey: "",
    count: 0,
  });

  const abortCurrentStream = useCallback(() => {
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
  }, []);

  useEffect(() => {
    if (!apiPort || !sessionId || !timelineReady) {
      abortCurrentStream();
      return;
    }

    abortCurrentStream();
    const controller = new AbortController();
    streamAbortRef.current = controller;
    const targetWorkspaceId = workspaceId;
    const targetSessionCacheKey = sessionCacheKey ?? sessionId;
    lastEventCursorRef.current = initialEventCursor;
    lastBusinessEventAtRef.current = Date.now();
    lastStaleProbeAtRef.current = 0;
    routeRevisionRef.current = null;
    let sessionListRefreshInFlight = false;
    // 游标失效恢复的作用域键：会话不变则沿用既有计数，切换会话才归零。
    const recoveryScopeKey = `${targetSessionCacheKey}::${sessionId}`;
    if (cursorGoneRecoveryRef.current.scopeKey !== recoveryScopeKey) {
      cursorGoneRecoveryRef.current = { scopeKey: recoveryScopeKey, count: 0 };
    }
    const refreshWorkspaceSessionsForStream = (force: boolean = false) => {
      if (
        !targetWorkspaceId
        || sessionListRefreshInFlight
        || (!force && document.visibilityState === "hidden")
      ) {
        return;
      }
      sessionListRefreshInFlight = true;
      void refreshWorkspaceSessionList(
        apiPort,
        targetWorkspaceId,
        setState,
      ).catch((error: unknown) => {
        const message = errorMessage(error);
        setState((latest) => ({
          ...latest,
          status: `刷新工作区会话失败: ${message}`,
        }));
      }).finally(() => {
        sessionListRefreshInFlight = false;
      });
    };
    // TODO: 工作区摘要事件流落地后删除这一低频完整快照兜底。
    const sessionListRefreshIntervalId = window.setInterval(
      refreshWorkspaceSessionsForStream,
      WORKSPACE_SESSION_FALLBACK_REFRESH_MS,
    );
    const refreshVisibleWorkspaceSessions = () => {
      if (document.visibilityState !== "hidden") {
        refreshWorkspaceSessionsForStream();
      }
    };
    document.addEventListener(
      "visibilitychange",
      refreshVisibleWorkspaceSessions,
    );
    window.addEventListener("focus", refreshVisibleWorkspaceSessions);
    window.addEventListener("online", refreshVisibleWorkspaceSessions);
    const pendingStreamEvents: SessionStreamEvent[] = [];
    let flushTimerId: number | null = null;

    const flushStreamEvents = () => {
      if (flushTimerId !== null) {
        window.clearTimeout(flushTimerId);
        flushTimerId = null;
      }
      const events = pendingStreamEvents.splice(0);
      if (events.length === 0 || controller.signal.aborted) {
        return;
      }
      flushSessionStreamEventBatch(events, {
        apiPort,
        sessionId,
        workspaceId: targetWorkspaceId,
        sessionCacheKey: targetSessionCacheKey,
        setState,
      });
    };

    const enqueueStreamEvent = (event: SessionStreamEvent, cursor: string) => {
      lastBusinessEventAtRef.current = Date.now();
      lastStaleProbeAtRef.current = 0;
      lastEventCursorRef.current = cursor;
      pendingStreamEvents.push(event);
      if (isJobTerminalTraceType(event.type)) {
        flushStreamEvents();
        void loadTerminalTurn(event.job_id).catch((error: unknown) => {
          if (controller.signal.aborted) return;
          const message = errorMessage(error);
          setState((latest) => ({
            ...latest,
            status: `加载已完成 Turn 失败: ${message}`,
          }));
        });
        return;
      }
      if (flushTimerId === null) {
        flushTimerId = window.setTimeout(flushStreamEvents, 32);
      }
    };

    const connect = async () => {
      let reconnectAttempt = 0;
      while (!controller.signal.aborted) {
        try {
          await streamSessionEvents(apiPort, sessionId, {
            workspaceId: targetWorkspaceId,
            afterCursor: lastEventCursorRef.current,
            signal: controller.signal,
            onEvent: enqueueStreamEvent,
            onActivity: () => {
              reconnectAttempt = 0;
              // 连接真正建立（收到任意字节，含心跳注释）说明游标已可用，
              // 允许下一轮失效重新走完整的有界恢复。
              cursorGoneRecoveryRef.current.count = 0;
            },
            onConnected: (routeRevision) => {
              const previousRevision = routeRevisionRef.current;
              routeRevisionRef.current = routeRevision;
              if (previousRevision !== routeRevision) {
                lastBusinessEventAtRef.current = Date.now();
              }
              if (
                previousRevision
                && routeRevision
                && previousRevision !== routeRevision
              ) {
                setState((previous) => ({
                  ...previous,
                  status: "工作区后端已换代，正在恢复实时事件流",
                }));
              }
            },
            idleTimeoutMs: SSE_IDLE_TIMEOUT_MS,
          });
        } catch (error) {
          if (controller.signal.aborted) {
            return;
          }
          if (error instanceof TraceCursorGoneError) {
            // 410 恢复不能像普通重连那样只靠局部计数：它会触发 Turn bootstrap
            // 重载并用 refreshTurnHistory 重建本 effect，局部计数每次都会被清零。
            // 因此这里累加 ref 计数并给出可见终态，杜绝「上游持续 410」的无上限风暴。
            cursorGoneRecoveryRef.current.count += 1;
            if (
              cursorGoneRecoveryRef.current.count
              >= SESSION_STREAM_MAX_RECONNECT_ATTEMPTS
            ) {
              setState((prev) => ({
                ...prev,
                status: `事件流连续 ${SESSION_STREAM_MAX_RECONNECT_ATTEMPTS} 次游标失效，已停止自动恢复；请手动刷新或切换会话后重试`,
              }));
              return;
            }
            setState((prev) => ({
              ...prev,
              status: "事件游标已失效，正在重新加载有界 Turn bootstrap",
            }));
            refreshTurnHistory();
            return;
          } else {
            const message = isTransientNetworkError(error)
              ? "本地服务连接暂时变化"
              : errorMessage(error);
            setState((prev) => ({
              ...prev,
              status: error instanceof SessionStreamIdleTimeoutError
                ? `事件流心跳超时，正在重连: ${message}`
                : `事件流断开，正在重连: ${message}`,
            }));
          }
        }

        if (controller.signal.aborted) {
          return;
        }
        // 有界重连：连续到达上限后停止重连并给出可见终态说明，不再无限刷屏。
        // onActivity 会在连接真正建立（收到任意字节，含心跳注释）时把计数归零，
        // 因此这里限制的是「连续若干次都没能建立连接」。
        if (reconnectAttempt >= SESSION_STREAM_MAX_RECONNECT_ATTEMPTS) {
          setState((prev) => ({
            ...prev,
            status: `事件流连续 ${SESSION_STREAM_MAX_RECONNECT_ATTEMPTS} 次重连失败，已停止自动重连；请手动刷新或切换会话后重试`,
          }));
          return;
        }
        await waitForReconnect(
          controller.signal,
          sessionStreamReconnectDelay(reconnectAttempt),
        );
        reconnectAttempt += 1;
      }
    };

    const connectTimerId = window.setTimeout(() => {
      void connect();
    }, 120);

    return () => {
      window.clearTimeout(connectTimerId);
      window.clearInterval(sessionListRefreshIntervalId);
      document.removeEventListener(
        "visibilitychange",
        refreshVisibleWorkspaceSessions,
      );
      window.removeEventListener("focus", refreshVisibleWorkspaceSessions);
      window.removeEventListener("online", refreshVisibleWorkspaceSessions);
      if (flushTimerId !== null) {
        window.clearTimeout(flushTimerId);
      }
      controller.abort();
    };
  }, [
    abortCurrentStream,
    apiPort,
    initialEventCursor,
    loadTerminalTurn,
    refreshTurnHistory,
    sessionCacheKey,
    sessionId,
    setState,
    timelineReady,
    workspaceId,
  ]);

  useEffect(() => {
    if (
      !apiPort
      || !sessionId
      || !sessionCacheKey
      || !activeJobId
    ) {
      return;
    }

    let reconciliationInFlight = false;
    const reconcile = () => {
      const now = Date.now();
      if (
        reconciliationInFlight
        || document.visibilityState === "hidden"
        || now - lastBusinessEventAtRef.current < ACTIVE_JOB_TRACE_STALE_MS
        || (
          lastStaleProbeAtRef.current > 0
          && now - lastStaleProbeAtRef.current
            < ACTIVE_JOB_STALE_PROBE_INTERVAL_MS
        )
      ) {
        return;
      }
      lastStaleProbeAtRef.current = now;
      reconciliationInFlight = true;
      void reconcileActiveJob(
        apiPort,
        sessionId,
        workspaceId,
        sessionCacheKey,
        activeJobId,
        setState,
        {
          afterCursor: lastEventCursorRef.current,
        },
      ).then((result) => {
        if (result.lastEventCursor) {
          lastEventCursorRef.current = result.lastEventCursor;
        }
        if (result.recoveredEventCount > 0) {
          lastBusinessEventAtRef.current = Date.now();
          lastStaleProbeAtRef.current = 0;
        }
      }).catch((error: unknown) => {
        const message = errorMessage(error);
        setState((latest) => ({
          ...latest,
          status: `对账运行中任务失败: ${message}`,
        }));
      }).finally(() => {
        reconciliationInFlight = false;
      });
    };
    const reconcileWhenVisible = () => {
      if (document.visibilityState !== "hidden") {
        reconcile();
      }
    };
    const intervalId = window.setInterval(
      reconcile,
      ACTIVE_JOB_RECONCILE_INTERVAL_MS,
    );
    document.addEventListener("visibilitychange", reconcileWhenVisible);
    window.addEventListener("online", reconcileWhenVisible);

    return () => {
      window.clearInterval(intervalId);
      document.removeEventListener("visibilitychange", reconcileWhenVisible);
      window.removeEventListener("online", reconcileWhenVisible);
    };
  }, [
    activeJobId,
    apiPort,
    sessionCacheKey,
    sessionId,
    setState,
    workspaceId,
  ]);

  return { abortCurrentStream };
}

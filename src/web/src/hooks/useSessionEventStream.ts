import {
  useCallback,
  useEffect,
  useRef,
  type Dispatch,
  type SetStateAction,
} from "react";
import {
  getSession,
  SessionStreamIdleTimeoutError,
  streamSessionEvents,
  TraceCursorGoneError,
  type SessionStreamEvent,
} from "../api";
import { listPendingRequests } from "../pendingRequestsApi";
import { cloneMaps } from "../state/appStateMaps";
import {
  updateAttachmentSummariesFromTraces,
} from "../state/attachments";
import {
  conversationMatchesTraceEvent,
  writePendingSnapshot,
  writePendingList,
} from "../state/conversations";
import {
  appendReceivedEvents,
  buildTraceEvent,
  dedupeTraceEvents,
  isJobTerminalTraceType,
  isTerminalTraceType,
  terminalStatusForEvent,
  tracePayloadString,
} from "../state/traceEvents";
import { replaceSessionMetadata } from "../state/session/sessions";
import { sessionScopeKey } from "../state/session/sessionScope";
import type { AppState, ConversationView } from "../types/frontend";
import {
  ACTIVE_JOB_RECONCILE_INTERVAL_MS,
  SESSION_STREAM_IDLE_TIMEOUT_MS,
  WORKSPACE_SESSION_FALLBACK_REFRESH_MS,
  sessionStreamReconnectDelay,
} from "./sessionEventStreamPolicy";
import {
  fetchWorkspaceSessionListSnapshot,
  isCurrentWorkspaceSessionListSnapshot,
} from "./workspaceSessionListRefresh";
import {
  recoverTraceSnapshot,
  waitForReconnect,
} from "./sessionEventStreamRecovery";
import {
  reconcileActiveJob,
  refreshTerminalSession,
} from "./sessionJobReconciliation";

type SetAppState = Dispatch<SetStateAction<AppState>>;

function sessionListsMatch(
  left: AppState["sessions"],
  right: AppState["sessions"],
): boolean {
  return left.length === right.length && left.every((session, index) => {
    const candidate = right[index];
    return candidate !== undefined
      && session.session_id === candidate.session_id
      && session.updated_at === candidate.updated_at;
  });
}

async function refreshSessionMetadata(
  apiPort: number,
  sessionId: string,
  workspaceId: string | null,
  sessionCacheKey: string,
  setState: SetAppState,
  announceAutoTitle: boolean = true,
) {
  const updatedSession = await getSession(apiPort, sessionId, workspaceId);
  setState((prev) => {
    if (workspaceId && prev.currentSessionWorkspaceId !== workspaceId) {
      return prev;
    }
    const next = replaceSessionMetadata(prev, updatedSession, workspaceId);
    next.currentSessionWorkspaceId = workspaceId ?? next.currentSessionWorkspaceId;
    if (
      announceAutoTitle &&
      prev.currentSession?.session_id === updatedSession.session_id
    ) {
      next.status = `已自动命名会话: ${updatedSession.title}`;
    }
    if (workspaceId) {
      next.sessionGatewayWorkspaceById.set(sessionCacheKey, workspaceId);
    }
    return next;
  });
}

async function refreshWorkspaceSessionList(
  apiPort: number,
  workspaceId: string | null,
  setState: SetAppState,
) {
  if (!workspaceId) {
    throw new Error("刷新委派子会话时缺少 workspace_id");
  }
  const snapshot = await fetchWorkspaceSessionListSnapshot(apiPort, workspaceId);
  if (!isCurrentWorkspaceSessionListSnapshot(snapshot)) {
    return;
  }
  setState((previous) => {
    if (!isCurrentWorkspaceSessionListSnapshot(snapshot)) {
      return previous;
    }
    const previousWorkspaceSessions =
      previous.sessionsByWorkspace.get(workspaceId) ?? [];
    if (sessionListsMatch(previousWorkspaceSessions, snapshot.sessions)) {
      return previous;
    }
    const next = cloneMaps(previous);
    const resolvedWorkspaceId = workspaceId;
    next.sessionsByWorkspace.set(resolvedWorkspaceId, snapshot.sessions);
    for (const session of snapshot.sessions) {
      next.sessionGatewayWorkspaceById.set(
        sessionScopeKey(resolvedWorkspaceId, session.session_id),
        resolvedWorkspaceId,
      );
    }
    if (
      previous.activeGatewayWorkspaceId === resolvedWorkspaceId ||
      previous.currentSessionWorkspaceId === resolvedWorkspaceId
    ) {
      next.sessions = snapshot.sessions;
    }
    return next;
  });
}

export function useSessionEventStream({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  activeJobId,
  setState,
}: {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  activeJobId: string | null;
  setState: SetAppState;
}) {
  const streamAbortRef = useRef<AbortController | null>(null);
  const lastEventIdRef = useRef<string | null>(null);

  const abortCurrentStream = useCallback(() => {
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
  }, []);

  useEffect(() => {
    if (!apiPort || !sessionId) {
      abortCurrentStream();
      return;
    }

    abortCurrentStream();
    const controller = new AbortController();
    streamAbortRef.current = controller;
    const targetWorkspaceId = workspaceId;
    const targetSessionCacheKey = sessionCacheKey ?? sessionId;
    lastEventIdRef.current = null;
    let sessionListRefreshInFlight = false;
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
        const message = error instanceof Error ? error.message : String(error);
        setState((latest) => ({
          ...latest,
          status: `刷新工作区会话失败: ${message}`,
        }));
      }).finally(() => {
        sessionListRefreshInFlight = false;
      });
    };
    refreshWorkspaceSessionsForStream(true);
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
      const traceEvents = events.map(buildTraceEvent);

      setState((prev) => {
        if (prev.currentSession?.session_id !== sessionId) {
          return prev;
        }
        if (
          targetWorkspaceId &&
          prev.currentSessionWorkspaceId !== targetWorkspaceId
        ) {
          return prev;
        }
        const next = cloneMaps(prev);
        next.traceEvents = dedupeTraceEvents([...next.traceEvents, ...traceEvents]);
        updateAttachmentSummariesFromTraces(
          next.sessionAttachmentSummaries,
          sessionId,
          traceEvents,
        );
        appendReceivedEvents(
          next.eventQueuesBySession,
          sessionId,
          traceEvents,
          "sse",
          targetSessionCacheKey,
        );

        for (const [index, traceEvent] of traceEvents.entries()) {
          const event = events[index];
          const pendingList =
            next.pendingConversations.get(targetSessionCacheKey) ?? [];
          if (pendingList.length === 0) {
            continue;
          }
          let pendingIndex = pendingList.findIndex((conversation) =>
            conversationMatchesTraceEvent(conversation, traceEvent),
          );
          if (pendingIndex === -1 && pendingList.length === 1) {
            pendingIndex = 0;
          }
          if (pendingIndex === -1) {
            continue;
          }

          const pending = pendingList[pendingIndex];
          const updatedPending: ConversationView = {
            ...pending,
            events: dedupeTraceEvents([...pending.events, traceEvent]),
          };
          if (event.type === "status_change") {
            const status = tracePayloadString(traceEvent, "status");
            updatedPending.status = status === "queued" ? "queued" : "running";
          } else if (
            [
              "job_started",
              "text_start",
              "text_delta",
              "text_end",
              "tool_call_start",
              "tool_call_end",
            ].includes(event.type)
          ) {
            updatedPending.status = "running";
          } else if (isTerminalTraceType(event.type)) {
            updatedPending.status = terminalStatusForEvent(event.type);
            updatedPending.pending = false;
          }

          const updatedPendingList = [...pendingList];
          updatedPendingList[pendingIndex] = updatedPending;
          writePendingList(
            next.pendingConversations,
            targetSessionCacheKey,
            updatedPendingList,
          );
        }
        return next;
      });

      const titleEventIndex = events.findIndex(
        (event, index) =>
          event.type === "status_change" &&
          tracePayloadString(traceEvents[index], "reason") ===
            "session_auto_title_updated",
      );
      if (titleEventIndex !== -1) {
        void refreshSessionMetadata(
          apiPort,
          sessionId,
          targetWorkspaceId,
          targetSessionCacheKey,
          setState,
        ).catch((error: unknown) => {
          const message = error instanceof Error ? error.message : String(error);
          setState((latest) => ({
            ...latest,
            status: `刷新会话标题失败: ${message}`,
          }));
        });
      }
      const delegatedSessionCreated = events.some(
        (event, index) =>
          event.type === "tool_call_end" &&
          tracePayloadString(traceEvents[index], "tool_name") === "task",
      );
      if (delegatedSessionCreated) {
        void refreshWorkspaceSessionList(
          apiPort,
          targetWorkspaceId,
          setState,
        ).catch((error: unknown) => {
          const message = error instanceof Error ? error.message : String(error);
          setState((latest) => ({
            ...latest,
            status: `刷新委派子会话失败: ${message}`,
          }));
        });
      }
      const pendingQueueChanged = events.some(
        (event, index) =>
          event.type === "status_change"
          && tracePayloadString(traceEvents[index], "reason").startsWith(
            "pending_request",
          ),
      );
      if (pendingQueueChanged) {
        void listPendingRequests(
          apiPort,
          sessionId,
          targetWorkspaceId,
        ).then((snapshot) => {
          setState((latest) => {
            const next = cloneMaps(latest);
            writePendingSnapshot(
              next.pendingConversations,
              next.activeJobIdsBySession,
              snapshot,
              targetSessionCacheKey,
            );
            return next;
          });
        }).catch((error: unknown) => {
          const message = error instanceof Error ? error.message : String(error);
          setState((latest) => ({
            ...latest,
            status: `刷新待处理消息失败: ${message}`,
          }));
        });
      }
      let terminalEventIndex = -1;
      for (let index = events.length - 1; index >= 0; index -= 1) {
        if (isJobTerminalTraceType(events[index].type)) {
          terminalEventIndex = index;
          break;
        }
      }
      if (terminalEventIndex !== -1) {
        void refreshTerminalSession(
          apiPort,
          sessionId,
          targetWorkspaceId,
          targetSessionCacheKey,
          traceEvents[terminalEventIndex] ?? null,
          setState,
        ).catch((error: unknown) => {
          const message = error instanceof Error ? error.message : String(error);
          setState((latest) => ({
            ...latest,
            status: `刷新失败: ${message}`,
          }));
        });
      }
    };

    const enqueueStreamEvent = (event: SessionStreamEvent) => {
      if (event.event_id) {
        lastEventIdRef.current = event.event_id;
      }
      pendingStreamEvents.push(event);
      if (isJobTerminalTraceType(event.type)) {
        flushStreamEvents();
        return;
      }
      if (flushTimerId === null) {
        flushTimerId = window.setTimeout(flushStreamEvents, 32);
      }
    };

    const connect = async () => {
      let snapshotLoaded = false;
      let reconnectAttempt = 0;
      while (!controller.signal.aborted && !snapshotLoaded) {
        try {
          const recovered = await recoverTraceSnapshot(
            apiPort,
            sessionId,
            targetWorkspaceId,
            targetSessionCacheKey,
            setState,
            "事件历史加载完成",
          );
          lastEventIdRef.current = recovered.lastEventId;
          await refreshSessionMetadata(
            apiPort,
            sessionId,
            targetWorkspaceId,
            targetSessionCacheKey,
            setState,
            false,
          );
          snapshotLoaded = true;
          reconnectAttempt = 0;
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          setState((prev) => ({
            ...prev,
            status: `加载事件历史失败，正在重试: ${message}`,
          }));
          await waitForReconnect(
            controller.signal,
            sessionStreamReconnectDelay(reconnectAttempt),
          );
          reconnectAttempt += 1;
        }
      }

      while (!controller.signal.aborted) {
        try {
          await streamSessionEvents(apiPort, sessionId, {
            workspaceId: targetWorkspaceId,
            afterEventId: lastEventIdRef.current,
            signal: controller.signal,
            onEvent: enqueueStreamEvent,
            onActivity: () => {
              reconnectAttempt = 0;
            },
            idleTimeoutMs: SESSION_STREAM_IDLE_TIMEOUT_MS,
          });
        } catch (error) {
          if (controller.signal.aborted) {
            return;
          }
          if (error instanceof TraceCursorGoneError) {
            try {
              const recovered = await recoverTraceSnapshot(
                apiPort,
                sessionId,
                targetWorkspaceId,
                targetSessionCacheKey,
                setState,
                "事件游标已恢复，正在继续接收",
              );
              lastEventIdRef.current = recovered.lastEventId;
              reconnectAttempt = 0;
            } catch (recoveryError) {
              const message =
                recoveryError instanceof Error
                  ? recoveryError.message
                  : String(recoveryError);
              setState((prev) => ({
                ...prev,
                status: `恢复事件历史失败: ${message}`,
              }));
            }
          } else {
            const message = error instanceof Error ? error.message : String(error);
            setState((prev) => ({
              ...prev,
              status: error instanceof SessionStreamIdleTimeoutError
                ? `事件流心跳超时，正在重连: ${message}`
                : `事件流断开，正在重连: ${message}`,
            }));
          }
        }

        if (!controller.signal.aborted) {
          await waitForReconnect(
            controller.signal,
            sessionStreamReconnectDelay(reconnectAttempt),
          );
          reconnectAttempt += 1;
        }
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
    sessionCacheKey,
    sessionId,
    setState,
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
      if (
        reconciliationInFlight
        || document.visibilityState === "hidden"
      ) {
        return;
      }
      reconciliationInFlight = true;
      void reconcileActiveJob(
        apiPort,
        sessionId,
        workspaceId,
        sessionCacheKey,
        activeJobId,
        setState,
      ).catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
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

import {
  useCallback,
  useEffect,
  useRef,
  type Dispatch,
  type SetStateAction,
} from "react";
import {
  getSession,
  listMessages,
  streamSessionEvents,
  TraceCursorGoneError,
  type SessionStreamEvent,
} from "../api";
import { cloneMaps } from "../state/appStateMaps";
import {
  updateAttachmentSummariesFromMessages,
  updateAttachmentSummariesFromTraces,
} from "../state/attachments";
import {
  conversationMatchesTraceEvent,
  removePendingForTraceEvent,
  writePendingList,
} from "../state/conversations";
import {
  appendReceivedEvents,
  buildTraceEvent,
  dedupeTraceEvents,
  isJobTerminalTraceType,
  isTerminalTraceType,
  terminalStatusTextForEvent,
  terminalStatusForEvent,
  tracePayloadString,
} from "../state/traceEvents";
import { replaceSessionMetadata } from "../state/sessions";
import type { AppState, ConversationView } from "../types/frontend";
import {
  recoverTraceSnapshot,
  waitForReconnect,
} from "./sessionEventStreamRecovery";

type SetAppState = Dispatch<SetStateAction<AppState>>;

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

async function refreshTerminalSession(
  apiPort: number,
  sessionId: string,
  workspaceId: string | null,
  sessionCacheKey: string,
  terminalTraceEvent: ReturnType<typeof buildTraceEvent>,
  setState: SetAppState,
) {
  const [messages, updatedSession] = await Promise.all([
    listMessages(apiPort, sessionId, workspaceId),
    getSession(apiPort, sessionId, workspaceId),
  ]);
  setState((latest) => {
    if (workspaceId && latest.currentSessionWorkspaceId !== workspaceId) {
      return latest;
    }
    const latestNext = replaceSessionMetadata(latest, updatedSession, workspaceId);
    latestNext.currentSessionWorkspaceId =
      workspaceId ?? latestNext.currentSessionWorkspaceId;
    removePendingForTraceEvent(
      latestNext.pendingConversations,
      sessionCacheKey,
      terminalTraceEvent,
    );
    if (latest.currentSession?.session_id !== sessionId) {
      return latestNext;
    }
    latestNext.messages = messages.items;
    updateAttachmentSummariesFromMessages(
      latestNext.sessionAttachmentSummaries,
      latestNext.messages,
    );
    latestNext.status = terminalStatusTextForEvent(terminalTraceEvent.type);
    return latestNext;
  });
}

export function useSessionEventStream({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  setState,
}: {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
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
          traceEvents[terminalEventIndex],
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
      while (!controller.signal.aborted && !snapshotLoaded) {
        try {
          lastEventIdRef.current = await recoverTraceSnapshot(
            apiPort,
            sessionId,
            targetWorkspaceId,
            targetSessionCacheKey,
            setState,
            "事件历史加载完成",
          );
          await refreshSessionMetadata(
            apiPort,
            sessionId,
            targetWorkspaceId,
            targetSessionCacheKey,
            setState,
            false,
          );
          snapshotLoaded = true;
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          setState((prev) => ({
            ...prev,
            status: `加载事件历史失败，正在重试: ${message}`,
          }));
          await waitForReconnect(controller.signal, 500);
        }
      }

      while (!controller.signal.aborted) {
        try {
          await streamSessionEvents(apiPort, sessionId, {
            workspaceId: targetWorkspaceId,
            afterEventId: lastEventIdRef.current,
            signal: controller.signal,
            onEvent: enqueueStreamEvent,
          });
        } catch (error) {
          if (controller.signal.aborted) {
            return;
          }
          if (error instanceof TraceCursorGoneError) {
            try {
              lastEventIdRef.current = await recoverTraceSnapshot(
                apiPort,
                sessionId,
                targetWorkspaceId,
                targetSessionCacheKey,
                setState,
                "事件游标已恢复，正在继续接收",
              );
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
              status: `事件流断开，正在重连: ${message}`,
            }));
          }
        }

        if (!controller.signal.aborted) {
          await waitForReconnect(controller.signal, 500);
        }
      }
    };

    const connectTimerId = window.setTimeout(() => {
      void connect();
    }, 120);

    return () => {
      window.clearTimeout(connectTimerId);
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

  return { abortCurrentStream };
}

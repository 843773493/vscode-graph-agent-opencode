import {
  useCallback,
  useEffect,
  useRef,
  type Dispatch,
  type SetStateAction,
} from "react";
import {
  getSessionTraces,
  listMessages,
  streamSessionEvents,
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
  terminalStatusForEvent,
  tracePayloadString,
} from "../state/traceEvents";
import type { AppState, ConversationView } from "../types/frontend";

type SetAppState = Dispatch<SetStateAction<AppState>>;

export function useSessionEventStream({
  apiPort,
  sessionId,
  setState,
}: {
  apiPort: number | null;
  sessionId: string | null;
  setState: SetAppState;
}) {
  const streamAbortRef = useRef<AbortController | null>(null);

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

    void streamSessionEvents(apiPort, sessionId, {
      signal: controller.signal,
      onEvent: (event: SessionStreamEvent) => {
        const traceEvent = buildTraceEvent(event);

        setState((prev) => {
          if (prev.currentSession?.session_id !== sessionId) {
            return prev;
          }
          const next = cloneMaps(prev);
          next.traceEvents = dedupeTraceEvents([...next.traceEvents, traceEvent]);
          updateAttachmentSummariesFromTraces(
            next.sessionAttachmentSummaries,
            sessionId,
            [traceEvent],
          );
          appendReceivedEvents(
            next.eventQueuesBySession,
            sessionId,
            [traceEvent],
            "sse",
          );

          const pendingList = next.pendingConversations.get(sessionId) ?? [];
          if (pendingList.length === 0) {
            return next;
          }

          let pendingIndex = pendingList.findIndex((conversation) =>
            conversationMatchesTraceEvent(conversation, traceEvent),
          );
          if (pendingIndex === -1 && pendingList.length === 1) {
            pendingIndex = 0;
          }
          if (pendingIndex === -1) {
            return next;
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
          writePendingList(next.pendingConversations, sessionId, updatedPendingList);

          if (isJobTerminalTraceType(event.type)) {
            const terminalTraceEvent = traceEvent;

            void (async () => {
              try {
                const [messages, traceEvents] = await Promise.all([
                  listMessages(apiPort, sessionId),
                  getSessionTraces(apiPort, sessionId),
                ]);
                setState((latest) => {
                  const latestNext = cloneMaps(latest);
                  removePendingForTraceEvent(
                    latestNext.pendingConversations,
                    sessionId,
                    terminalTraceEvent,
                  );
                  if (latest.currentSession?.session_id !== sessionId) {
                    return latestNext;
                  }
                  latestNext.messages = messages.items;
                  const refreshedTraceEvents = dedupeTraceEvents(traceEvents);
                  latestNext.traceEvents = refreshedTraceEvents;
                  updateAttachmentSummariesFromMessages(
                    latestNext.sessionAttachmentSummaries,
                    latestNext.messages,
                  );
                  updateAttachmentSummariesFromTraces(
                    latestNext.sessionAttachmentSummaries,
                    sessionId,
                    refreshedTraceEvents,
                  );
                  appendReceivedEvents(
                    latestNext.eventQueuesBySession,
                    sessionId,
                    refreshedTraceEvents,
                    "terminal_refresh",
                  );
                  latestNext.status = "消息已更新";
                  return latestNext;
                });
              } catch (error) {
                const message =
                  error instanceof Error ? error.message : String(error);
                setState((latest) => ({
                  ...latest,
                  status: `刷新失败: ${message}`,
                }));
              }
            })();
          }

          return next;
        });
      },
      onError: (error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `事件流错误: ${message}` }));
      },
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `事件流错误: ${message}` }));
      }
    });

    return () => {
      controller.abort();
    };
  }, [abortCurrentStream, apiPort, sessionId, setState]);

  return { abortCurrentStream };
}

import { useEffect } from "react";
import {
  getSessionTraces,
  listMessages,
} from "../api";
import { cloneMaps } from "../state/appStateMaps";
import {
  updateAttachmentSummariesFromMessages,
  updateAttachmentSummariesFromTraces,
} from "../state/attachments";
import {
  appendFrontendEvent,
  appendReceivedEvents,
  dedupeTraceEvents,
} from "../state/traceEvents";
import type { SetAppState } from "./contentViewLoaderTypes";

export function useSessionHistoryLoader({
  apiPort,
  sessionId,
  setState,
}: {
  apiPort: number | null;
  sessionId: string | null;
  setState: SetAppState;
}) {
  useEffect(() => {
    if (!apiPort || !sessionId) return;

    let cancelled = false;
    setState((prev) => {
      if (prev.currentSession?.session_id !== sessionId) return prev;
      const next = cloneMaps(prev);
      appendFrontendEvent(
        next.eventQueuesBySession,
        sessionId,
        "session_load_started",
        "开始加载会话历史",
        { session_id: sessionId },
      );
      return next;
    });

    void (async () => {
      try {
        const [messages, traceEvents] = await Promise.all([
          listMessages(apiPort, sessionId),
          getSessionTraces(apiPort, sessionId),
        ]);
        if (cancelled) return;
        setState((prev) => {
          if (prev.currentSession?.session_id !== sessionId) return prev;
          const next = cloneMaps(prev);
          const fetchedTraceEvents = dedupeTraceEvents(traceEvents);
          next.messages = messages.items ?? [];
          next.traceEvents = fetchedTraceEvents;
          updateAttachmentSummariesFromMessages(
            next.sessionAttachmentSummaries,
            next.messages,
          );
          updateAttachmentSummariesFromTraces(
            next.sessionAttachmentSummaries,
            sessionId,
            fetchedTraceEvents,
          );
          appendReceivedEvents(
            next.eventQueuesBySession,
            sessionId,
            fetchedTraceEvents,
            "initial_load",
          );
          appendFrontendEvent(
            next.eventQueuesBySession,
            sessionId,
            "session_load_completed",
            "会话历史加载完成",
            {
              session_id: sessionId,
              message_count: messages.items?.length ?? 0,
              trace_event_count: fetchedTraceEvents.length,
            },
          );
          return next;
        });
      } catch (error) {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => {
          if (prev.currentSession?.session_id !== sessionId) {
            return prev;
          }
          const next = cloneMaps(prev);
          next.status = `加载失败: ${message}`;
          appendFrontendEvent(
            next.eventQueuesBySession,
            sessionId,
            "session_load_failed",
            "会话历史加载失败",
            { session_id: sessionId, error: message },
            message,
          );
          return next;
        });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [apiPort, sessionId, setState]);
}

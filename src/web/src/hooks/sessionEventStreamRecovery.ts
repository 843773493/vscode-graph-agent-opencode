import { getSessionTraces } from "../api";
import { cloneMaps } from "../state/appStateMaps";
import { updateAttachmentSummariesFromTraces } from "../state/attachments";
import {
  hasJobTerminalTraceEvent,
  traceEventsForConversation,
  writePendingList,
} from "../state/conversations";
import {
  appendReceivedEvents,
  dedupeTraceEvents,
} from "../state/traceEvents";
import type { SetAppState } from "./contentViewLoaderTypes";

export async function recoverTraceSnapshot(
  apiPort: number,
  sessionId: string,
  workspaceId: string | null,
  sessionCacheKey: string,
  setState: SetAppState,
  statusText: string,
): Promise<string | null> {
  const traceEvents = await getSessionTraces(apiPort, sessionId, workspaceId);
  const lastEventId = traceEvents[traceEvents.length - 1]?.event_id ?? null;
  const recoveredTraceEvents = dedupeTraceEvents(traceEvents);

  setState((prev) => {
    if (prev.currentSession?.session_id !== sessionId) {
      return prev;
    }
    if (workspaceId && prev.currentSessionWorkspaceId !== workspaceId) {
      return prev;
    }

    const next = cloneMaps(prev);
    next.traceEvents = recoveredTraceEvents;
    updateAttachmentSummariesFromTraces(
      next.sessionAttachmentSummaries,
      sessionId,
      recoveredTraceEvents,
    );
    appendReceivedEvents(
      next.eventQueuesBySession,
      sessionId,
      recoveredTraceEvents,
      "initial_load",
      sessionCacheKey,
    );
    const pendingList = next.pendingConversations.get(sessionCacheKey) ?? [];
    writePendingList(
      next.pendingConversations,
      sessionCacheKey,
      pendingList.filter((conversation) => {
        const events = traceEventsForConversation(
          recoveredTraceEvents,
          conversation,
        );
        return !hasJobTerminalTraceEvent(events);
      }),
    );
    next.status = statusText;
    return next;
  });

  return lastEventId;
}

export function waitForReconnect(
  signal: AbortSignal,
  delayMs: number,
): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timerId);
      resolve();
    };
    const timerId = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

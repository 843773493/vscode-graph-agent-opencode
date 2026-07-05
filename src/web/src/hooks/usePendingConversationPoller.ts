import { useEffect, type Dispatch, type SetStateAction } from "react";
import { getSession, getSessionTraces, listMessages } from "../api";
import { cloneMaps } from "../state/appStateMaps";
import {
  updateAttachmentSummariesFromMessages,
  updateAttachmentSummariesFromTraces,
} from "../state/attachments";
import {
  hasJobTerminalTraceEvent,
  statusForConversationEvents,
  traceEventsForConversation,
  writePendingList,
} from "../state/conversations";
import { replaceSessionMetadata } from "../state/sessions";
import { appendReceivedEvents, dedupeTraceEvents } from "../state/traceEvents";
import type { AppState } from "../types/frontend";

type SetAppState = Dispatch<SetStateAction<AppState>>;

export function usePendingConversationPoller({
  apiPort,
  sessionId,
  pendingPollKey,
  setState,
}: {
  apiPort: number | null;
  sessionId: string | null;
  pendingPollKey: string;
  setState: SetAppState;
}) {
  useEffect(() => {
    if (!apiPort || !sessionId || !pendingPollKey) {
      return;
    }

    let cancelled = false;
    let timerId: number | null = null;

    const scheduleNext = () => {
      if (cancelled) {
        return;
      }
      timerId = window.setTimeout(refreshPendingFromBackend, 1000);
    };

    const refreshPendingFromBackend = async () => {
      try {
        const [messages, traceEvents, updatedSession] = await Promise.all([
          listMessages(apiPort, sessionId),
          getSessionTraces(apiPort, sessionId),
          getSession(apiPort, sessionId),
        ]);
        if (cancelled) {
          return;
        }

        const fetchedTraceEvents = dedupeTraceEvents(traceEvents);
        setState((prev) => {
          if (prev.currentSession?.session_id !== sessionId) {
            return prev;
          }

          const next = cloneMaps(prev);
          next.messages = messages.items ?? [];
          next.traceEvents = dedupeTraceEvents([
            ...next.traceEvents,
            ...fetchedTraceEvents,
          ]);
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
            "pending_poll",
          );

          const pendingList = next.pendingConversations.get(sessionId) ?? [];
          const updatedPendingList = pendingList
            .map((conversation) => {
              const conversationEvents = traceEventsForConversation(
                fetchedTraceEvents,
                conversation,
              );
              if (conversationEvents.length === 0) {
                return conversation;
              }

              const events = dedupeTraceEvents([
                ...conversation.events,
                ...conversationEvents,
              ]);
              return {
                ...conversation,
                events,
                status: statusForConversationEvents(
                  events,
                  conversation.status,
                ),
                pending: !hasJobTerminalTraceEvent(events),
              };
            })
            .filter((conversation) => conversation.pending);

          writePendingList(
            next.pendingConversations,
            sessionId,
            updatedPendingList,
          );

          if (updatedPendingList.length < pendingList.length) {
            const metadataNext = replaceSessionMetadata(next, updatedSession);
            metadataNext.status = "消息已更新";
            return metadataNext;
          }

          return next;
        });
      } catch (error) {
        if (cancelled) {
          return;
        }
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          status: `刷新运行中消息失败: ${message}`,
        }));
      } finally {
        scheduleNext();
      }
    };

    timerId = window.setTimeout(refreshPendingFromBackend, 500);

    return () => {
      cancelled = true;
      if (timerId !== null) {
        window.clearTimeout(timerId);
      }
    };
  }, [apiPort, sessionId, pendingPollKey, setState]);
}

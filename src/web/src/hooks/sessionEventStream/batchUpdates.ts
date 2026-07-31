import { listPendingRequests } from "../../pendingRequestsApi";
import { cloneMaps } from "../../state/appStateMaps";
import { updateAttachmentSummariesFromTraces } from "../../state/attachments";
import {
  appendTraceEventsToPendingConversations,
  writePendingSnapshot,
} from "../../state/conversations";
import { goalStreamMutation } from "../../state/sessionGoal";
import {
  appendBoundedLiveTraceEvents,
  appendReceivedEvents,
  buildTraceEvent,
  tracePayloadString,
} from "../../state/traceEvents";
import {
  dispatchWorkspaceFileChanges,
  fileChangesFromTraceEvents,
} from "../../state/workspaceFileTreeEvents";
import type { SessionStreamEvent } from "../../types/backend";
import { refreshTerminalSession } from "../sessionJobReconciliation";
import { planTurnRefreshes } from "./refreshPlan";
import {
  refreshSessionMetadata,
  refreshWorkspaceSessionList,
  type SetAppState,
} from "./sessionRefresh";

export interface SessionStreamBatchContext {
  apiPort: number;
  sessionId: string;
  workspaceId: string | null;
  sessionCacheKey: string;
  refreshTurnDetails: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
  ) => Promise<void>;
  setState: SetAppState;
}

function updateCurrentSessionFromEvents(
  events: readonly SessionStreamEvent[],
  context: SessionStreamBatchContext,
): ReturnType<typeof buildTraceEvent>[] {
  const traceEvents = events.map(buildTraceEvent);
  dispatchWorkspaceFileChanges(
    context.workspaceId,
    fileChangesFromTraceEvents(traceEvents),
  );
  context.setState((previous) => {
    if (previous.currentSession?.session_id !== context.sessionId) {
      return previous;
    }
    if (
      context.workspaceId
      && previous.currentSessionWorkspaceId !== context.workspaceId
    ) {
      return previous;
    }
    const next = cloneMaps(previous);
    next.traceEvents = appendBoundedLiveTraceEvents(next.traceEvents, traceEvents);
    updateAttachmentSummariesFromTraces(
      next.sessionAttachmentSummaries,
      context.sessionId,
      traceEvents,
    );
    appendReceivedEvents(
      next.eventQueuesBySession,
      context.sessionId,
      traceEvents,
      "sse",
      context.sessionCacheKey,
    );
    appendTraceEventsToPendingConversations(
      next.pendingConversations,
      context.sessionId,
      traceEvents,
      context.sessionCacheKey,
      true,
    );
    for (const event of events) {
      const mutation = goalStreamMutation(event);
      if (mutation?.kind === "updated") {
        next.currentGoal = mutation.goal;
        next.currentGoalSessionId = context.sessionId;
        next.goalLoading = false;
        next.goalError = null;
      } else if (mutation?.kind === "cleared") {
        next.currentGoal = null;
        next.currentGoalSessionId = context.sessionId;
        next.goalLoading = false;
        next.goalError = null;
      }
    }
    return next;
  });
  return traceEvents;
}

function setRefreshError(
  setState: SetAppState,
  prefix: string,
  error: unknown,
): void {
  const message = error instanceof Error ? error.message : String(error);
  setState((latest) => ({
    ...latest,
    status: `${prefix}: ${message}`,
  }));
}

export function flushSessionStreamEventBatch(
  events: readonly SessionStreamEvent[],
  context: SessionStreamBatchContext,
): void {
  if (events.length === 0) {
    return;
  }
  const traceEvents = updateCurrentSessionFromEvents(events, context);
  const titleEventIndex = events.findIndex(
    (event, index) =>
      event.type === "status_change"
      && tracePayloadString(traceEvents[index], "reason")
        === "session_auto_title_updated",
  );
  if (titleEventIndex !== -1) {
    void refreshSessionMetadata(
      context.apiPort,
      context.sessionId,
      context.workspaceId,
      context.sessionCacheKey,
      context.setState,
    ).catch((error: unknown) => {
      setRefreshError(context.setState, "刷新会话标题失败", error);
    });
  }
  const delegatedSessionCreated = events.some(
    (event, index) =>
      event.type === "tool_call_end"
      && tracePayloadString(traceEvents[index], "tool_name") === "task",
  );
  if (delegatedSessionCreated) {
    void refreshWorkspaceSessionList(
      context.apiPort,
      context.workspaceId,
      context.setState,
    ).catch((error: unknown) => {
      setRefreshError(context.setState, "刷新委派子会话失败", error);
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
      context.apiPort,
      context.sessionId,
      context.workspaceId,
    ).then((snapshot) => {
      context.setState((latest) => {
        const next = cloneMaps(latest);
        writePendingSnapshot(
          next.pendingConversations,
          next.activeJobIdsBySession,
          snapshot,
          context.sessionCacheKey,
        );
        return next;
      });
    }).catch((error: unknown) => {
      setRefreshError(context.setState, "刷新待处理消息失败", error);
    });
  }
  const { genericTurnIds, terminalEventIndex } = planTurnRefreshes(events);
  for (let index = 0; index < genericTurnIds.length; index += 4) {
    void context.refreshTurnDetails(
      genericTurnIds.slice(index, index + 4),
      null,
      true,
    ).catch(
      () => {
        // Turn hook 已把详情错误写入时间线，SSE 只负责发出失效信号。
      },
    );
  }
  if (terminalEventIndex !== -1) {
    void refreshTerminalSession(
      context.apiPort,
      context.sessionId,
      context.workspaceId,
      context.sessionCacheKey,
      traceEvents[terminalEventIndex] ?? null,
      (turnIds) => context.refreshTurnDetails(turnIds, null, true),
      context.setState,
    ).catch((error: unknown) => {
      setRefreshError(context.setState, "刷新失败", error);
    });
  }
}

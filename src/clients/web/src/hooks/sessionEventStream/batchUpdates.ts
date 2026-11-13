import { isTransientNetworkError } from "../../api/http";
import { listThreadPendingRequests } from "../../pendingRequestsApi";
import { cloneMaps } from "../../state/appStateMaps";
import { hasSessionOwner } from "../../state/session/sessions";
import { updateAttachmentSummariesFromTraces } from "../../state/attachments";
import {
  appendTraceEventsToPendingConversations,
  writePendingSnapshot,
  writeDispatchActiveJob,
} from "../../state/conversations";
import { goalStreamMutation } from "../../protocol/sessionGoal";
import { applyVersionedValue } from "../../../../shared/core/syncState";
import {
  appendBoundedLiveTraceEvents,
  appendReceivedEvents,
  buildTraceEvent,
  tracePayloadString,
  rawTracePayload,
} from "../../state/traceEvents";
import {
  dispatchWorkspaceFileChanges,
  fileChangesFromTraceEvents,
} from "../../state/workspaceFileTreeEvents";
import type { JobDispatchSnapshot, SessionStreamEvent } from "../../types/backend";
import {
  refreshSessionMetadata,
} from "./sessionRefresh";
import type { SetAppState } from "../contentViewLoaderTypes";
import { errorMessage } from "../../utils/errorMessage";

export interface SessionStreamBatchContext {
  apiPort: number;
  sessionId: string;
  threadId?: string;
  workspaceId: string | null;
  sessionCacheKey: string;
  setState: SetAppState;
  signal?: AbortSignal;
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
    const owner = {
      session_id: context.sessionId, thread_id: previous.currentSession.thread_id ?? "",
    };
    const goalMutations = events.map((event) => goalStreamMutation(event, owner));
    const next = cloneMaps(previous);
    next.traceEvents = appendBoundedLiveTraceEvents(next.traceEvents, traceEvents);
    updateAttachmentSummariesFromTraces(
      next.sessionAttachmentSummaries,
      context.sessionId,
      context.sessionCacheKey,
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
    for (const event of traceEvents) {
      if (event.type !== "job_started") continue;
      const dispatch = rawTracePayload(event).dispatch as JobDispatchSnapshot | undefined;
      if (!dispatch || dispatch.session_id !== context.sessionId || dispatch.thread_id !== (context.threadId ?? previous.currentSession.thread_id) || dispatch.job_id !== event.job_id) {
        throw new Error("Job 开始事件缺少同归属的队列水位");
      }
      writeDispatchActiveJob(next.activeJobIdsBySession, dispatch, context.sessionCacheKey, next.pendingQueueVersions);
    }
    for (const mutation of goalMutations) {
      if (mutation?.kind === "updated") {
        if (mutation.goal.sync_version < next.currentGoalSyncVersion) continue;
        next.currentGoal = applyVersionedValue(next.currentGoal, mutation.goal, (goal) => goal.sync_version);
        next.currentGoalSessionId = context.sessionId;
        next.currentGoalControlRevision = mutation.goal.control_revision;
        next.currentGoalSyncVersion = mutation.goal.sync_version;
        next.goalLoading = false;
        next.goalError = null;
      } else if (mutation?.kind === "cleared") {
        if (mutation.syncVersion < next.currentGoalSyncVersion) continue;
        next.currentGoal = null;
        next.currentGoalSessionId = context.sessionId;
        next.currentGoalControlRevision = mutation.controlRevision;
        next.currentGoalSyncVersion = mutation.syncVersion;
        next.goalLoading = false;
        next.goalError = null;
      }
    }
    return next;
  });
  return traceEvents;
}

function setRefreshError(
  context: SessionStreamBatchContext,
  prefix: string,
  error: unknown,
): void {
  const message = isTransientNetworkError(error)
    ? "本地服务连接暂时变化，已保留当前状态并自动重试"
    : errorMessage(error);
  context.setState((latest) => context.signal?.aborted || !hasSessionOwner(latest, context.sessionId, context.workspaceId) ? latest : ({
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
      true,
      context.signal,
    ).catch((error: unknown) => {
      setRefreshError(context, "刷新会话标题失败", error);
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
    void listThreadPendingRequests(
      context.apiPort,
      context.sessionId,
      context.workspaceId,
      context.signal,
      context.threadId,
    ).then((snapshot) => {
      context.setState((latest) => {
        if (context.signal?.aborted || !hasSessionOwner(latest, context.sessionId, context.workspaceId)) return latest;
        const next = cloneMaps(latest);
        writePendingSnapshot(
          next.pendingConversations,
          next.activeJobIdsBySession,
          snapshot,
          context.sessionCacheKey,
          next.pendingQueueVersions,
        );
        return next;
      });
    }).catch((error: unknown) => {
      setRefreshError(context, "刷新待处理消息失败", error);
    });
  }
}

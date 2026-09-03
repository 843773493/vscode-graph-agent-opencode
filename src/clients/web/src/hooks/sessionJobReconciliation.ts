import { getJob, getSession } from "../api";
import { listPendingRequests } from "../pendingRequestsApi";
import {
  preservePendingTerminalConversation,
  writePendingSnapshot,
} from "../state/conversations";
import {
  terminalStatusTextForEvent,
} from "../state/traceEvents";
import { cloneMaps } from "../state/appStateMaps";
import { replaceSessionMetadata } from "../state/session/sessions";
import { writeTurnTimelineCache } from "../state/session/turnTimeline";
import type {
  Job,
  JobStatus,
  PendingRequestList,
  TraceEvent,
} from "../types/backend";
import type { AppState } from "../types/frontend";
import type { SetAppState } from "./contentViewLoaderTypes";
import { sessionScopeKey } from "../state/session/sessionScope";

const TERMINAL_JOB_STATUSES = new Set<JobStatus>([
  "completed",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
]);

const terminalSessionRefreshes = new Map<string, Promise<void>>();

function terminalSessionRefreshKey(
  apiPort: number,
  sessionId: string,
  workspaceId: string | null,
  turnId: string,
): string {
  return `${apiPort}:${workspaceId ?? ""}:${sessionId}:${turnId}`;
}

export interface ActiveJobReconciliationResult {
  jobStatus: JobStatus;
  lastEventCursor: string | null;
  recoveredEventCount: number;
}

function sessionIsActivelyViewed(
  state: AppState,
  sessionCacheKey: string,
): boolean {
  const currentSessionId = state.currentSession?.session_id;
  const currentWorkspaceId = state.currentSessionWorkspaceId;
  if (!currentSessionId || !currentWorkspaceId) {
    return false;
  }
  if (sessionScopeKey(currentWorkspaceId, currentSessionId) !== sessionCacheKey) {
    return false;
  }
  return typeof document === "undefined"
    || (document.visibilityState === "visible" && document.hasFocus());
}

function terminalStatusTextForJob(job: Job | undefined): string {
  if (!job) {
    throw new Error("缺少终态 Job，无法更新会话状态");
  }
  if (!TERMINAL_JOB_STATUSES.has(job.status)) {
    throw new Error(`Job 尚未终止: job_id=${job.job_id} status=${job.status}`);
  }
  if (job.status === "timed_out") {
    return job.error_message
      ? `任务超时: ${job.error_message}`
      : "任务超时";
  }
  if (job.status === "failed") {
    return job.error_message
      ? `任务失败: ${job.error_message}`
      : `任务已终止: ${job.status}`;
  }
  if (job.status === "cancelled") {
    return "任务已取消";
  }
  return "任务已完成";
}

function terminalTurnStatusForJob(
  job: Job,
): Extract<JobStatus, "completed" | "failed" | "cancelled" | "timed_out"> {
  if (job.status === "completed" || job.status === "succeeded") return "completed";
  if (job.status === "cancelled") return "cancelled";
  if (job.status === "timed_out") return "timed_out";
  if (job.status === "failed") return "failed";
  throw new Error(`Job 尚未终止: job_id=${job.job_id} status=${job.status}`);
}

function isJobTimeoutEvent(event: TraceEvent): boolean {
  if (event.type !== "job_failed") return false;
  if (event.payload?.code === "job_timeout") return true;
  return typeof event.payload?.error === "string"
    && event.payload.error.includes("超过总超时上限");
}

function terminalEventForJob(job: Job): TraceEvent {
  const turnStatus = terminalTurnStatusForJob(job);
  const eventType = turnStatus === "completed"
    ? "job_completed"
    : turnStatus === "cancelled"
      ? "job_cancelled"
      : "job_failed";
  const error = job.error_message
    ?? (turnStatus === "timed_out" ? "任务执行超过总超时上限" : "任务失败");
  return {
    event_id: `reconciled:${job.job_id}:${eventType}`,
    session_id: job.session_id,
    job_id: job.job_id,
    type: eventType,
    phase: "job",
    title: turnStatus === "timed_out" ? "任务超时" : "任务已结束",
    content: error,
    status: turnStatus === "completed" ? "completed" : "failed",
    timestamp: job.ended_at ?? job.updated_at,
    skill_names: [],
    payload: {
      ...(turnStatus === "timed_out" ? { code: "job_timeout" } : {}),
      ...(turnStatus === "cancelled" ? {} : { error }),
    },
  };
}

function turnStatusForEvent(
  event: TraceEvent,
): Extract<JobStatus, "completed" | "failed" | "cancelled" | "timed_out"> {
  if (event.type === "job_completed") return "completed";
  if (event.type === "job_cancelled" || event.type === "session_interrupted") {
    return "cancelled";
  }
  if (event.type === "job_failed") {
    return isJobTimeoutEvent(event) ? "timed_out" : "failed";
  }
  throw new Error(`不是 Job 终态事件: ${event.type}`);
}

function preserveTerminalTurnFallback(
  state: AppState,
  sessionId: string,
  sessionCacheKey: string,
  terminalEvent: TraceEvent,
  turnStatus: Extract<JobStatus, "completed" | "failed" | "cancelled" | "timed_out">,
): AppState {
  const next = cloneMaps(state);
  preservePendingTerminalConversation(
    next.pendingConversations,
    sessionId,
    terminalEvent,
    turnStatus,
    sessionCacheKey,
  );
  // Job API 已确认终态后，实时镜像不再代表可运行任务，但要保留它作为
  // 当前视图的已完成回合；下一次 bootstrap 会用 canonical 历史替换它。
  next.activeJobIdsBySession.delete(sessionCacheKey);
  const timeline = next.turnTimelinesBySession.get(sessionCacheKey);
  const turn = timeline?.turnsById[terminalEvent.job_id];
  if (latestSessionMatches(next, sessionId)) {
    next.status = turnStatus === "completed"
      ? "任务已完成"
      : turnStatus === "cancelled"
        ? "任务已取消"
        : turnStatus === "timed_out"
          ? terminalEvent.content
            ? `任务超时: ${terminalEvent.content}`
            : "任务超时"
        : terminalEvent.content
          ? `任务失败: ${terminalEvent.content}`
          : "任务失败";
  }
  if (!timeline || !turn) return next;
  next.turnTimelinesBySession = writeTurnTimelineCache(
    next.turnTimelinesBySession,
    sessionCacheKey,
    {
      ...timeline,
      turnsById: {
        ...timeline.turnsById,
        [terminalEvent.job_id]: {
          ...turn,
          status: turnStatus,
          ...(turnStatus === "completed" && !turn.completed_at
            ? { completed_at: turn.updated_at }
            : {}),
        },
      },
    },
  );
  return next;
}

function latestSessionMatches(state: AppState, sessionId: string): boolean {
  return state.currentSession?.session_id === sessionId;
}

async function refreshTerminalSessionInternal(
  apiPort: number,
  sessionId: string,
  workspaceId: string | null,
  sessionCacheKey: string,
  terminalTraceEvent: TraceEvent | null,
  setState: SetAppState,
  knownPendingSnapshot?: PendingRequestList,
  knownTerminalJob?: Job,
) {
  const terminalTurnId = terminalTraceEvent?.job_id ?? knownTerminalJob?.job_id;
  const effectiveTerminalEvent = terminalTraceEvent
    ?? (knownTerminalJob ? terminalEventForJob(knownTerminalJob) : null);
  const terminalTurnStatus = effectiveTerminalEvent
    ? turnStatusForEvent(effectiveTerminalEvent)
    : null;

  // 终态先把 live 镜像标记为完成，避免继续显示“正在生成”。
  // 当前视图保留这个回合，下一次 bootstrap 再用 canonical 历史替换它。
  setState((latest) => {
    if (workspaceId && latest.currentSessionWorkspaceId !== workspaceId) {
      return latest;
    }
    if (!effectiveTerminalEvent || !terminalTurnStatus) return latest;
    return preserveTerminalTurnFallback(
      latest,
      sessionId,
      sessionCacheKey,
      effectiveTerminalEvent,
      terminalTurnStatus,
    );
  });

  const [updatedSession, fetchedPendingSnapshot] = await Promise.all([
    getSession(apiPort, sessionId, workspaceId),
    knownPendingSnapshot
      ? Promise.resolve(knownPendingSnapshot)
      : listPendingRequests(apiPort, sessionId, workspaceId),
  ]);
  const pendingSnapshot = fetchedPendingSnapshot.active_job_id === terminalTurnId
    ? { ...fetchedPendingSnapshot, active_job_id: null }
    : fetchedPendingSnapshot;
  setState((latest) => {
    if (workspaceId && latest.currentSessionWorkspaceId !== workspaceId) {
      return latest;
    }
    let latestNext = replaceSessionMetadata(latest, updatedSession, workspaceId);
    latestNext.currentSessionWorkspaceId =
      workspaceId ?? latestNext.currentSessionWorkspaceId;
    if (effectiveTerminalEvent && terminalTurnStatus) {
      latestNext = preserveTerminalTurnFallback(
        latestNext,
        sessionId,
        sessionCacheKey,
        effectiveTerminalEvent,
        terminalTurnStatus,
      );
    }
    writePendingSnapshot(
      latestNext.pendingConversations,
      latestNext.activeJobIdsBySession,
      pendingSnapshot,
      sessionCacheKey,
    );
    // 保留已完成的 live 回合，直到下一次 bootstrap 以 canonical Turn 替换它。
    if (
      !pendingSnapshot.active_job_id
      && (pendingSnapshot.requests?.length ?? 0) === 0
    ) {
      if (sessionIsActivelyViewed(latest, sessionCacheKey)) {
        latestNext.unreadSessionKeys.delete(sessionCacheKey);
      } else {
        latestNext.unreadSessionKeys.add(sessionCacheKey);
      }
    }
    if (latest.currentSession?.session_id !== sessionId) {
      return latestNext;
    }
    latestNext.status = terminalTraceEvent
      ? isJobTimeoutEvent(terminalTraceEvent)
        ? "任务超时"
        : terminalStatusTextForEvent(terminalTraceEvent.type)
      : terminalStatusTextForJob(knownTerminalJob);
    return latestNext;
  });
}

export function refreshTerminalSession(
  apiPort: number,
  sessionId: string,
  workspaceId: string | null,
  sessionCacheKey: string,
  terminalTraceEvent: TraceEvent | null,
  setState: SetAppState,
  knownPendingSnapshot?: PendingRequestList,
  knownTerminalJob?: Job,
): Promise<void> {
  const terminalTurnId = terminalTraceEvent?.job_id ?? knownTerminalJob?.job_id;
  if (!terminalTurnId) {
    return refreshTerminalSessionInternal(
      apiPort,
      sessionId,
      workspaceId,
      sessionCacheKey,
      terminalTraceEvent,
      setState,
      knownPendingSnapshot,
      knownTerminalJob,
    );
  }

  const key = terminalSessionRefreshKey(
    apiPort,
    sessionId,
    workspaceId,
    terminalTurnId,
  );
  const existing = terminalSessionRefreshes.get(key);
  if (existing) {
    return existing;
  }
  const request = refreshTerminalSessionInternal(
    apiPort,
    sessionId,
    workspaceId,
    sessionCacheKey,
    terminalTraceEvent,
    setState,
    knownPendingSnapshot,
    knownTerminalJob,
  );
  const trackedRequest = request.finally(() => {
    if (terminalSessionRefreshes.get(key) === trackedRequest) {
      terminalSessionRefreshes.delete(key);
    }
  });
  terminalSessionRefreshes.set(key, trackedRequest);
  return trackedRequest;
}

export async function reconcileActiveJob(
  apiPort: number,
  sessionId: string,
  workspaceId: string | null,
  sessionCacheKey: string,
  activeJobId: string,
  setState: SetAppState,
  options?: {
    afterCursor?: string | null;
  },
): Promise<ActiveJobReconciliationResult> {
  const job = await getJob(apiPort, activeJobId, workspaceId);
  if (!TERMINAL_JOB_STATUSES.has(job.status)) {
    return {
      jobStatus: job.status,
      lastEventCursor: options?.afterCursor ?? null,
      recoveredEventCount: 0,
    };
  }

  const pendingSnapshot = await listPendingRequests(apiPort, sessionId, workspaceId);
  // Job API 是终态的权威来源。Gateway/旧 SSE 可能在 Job 已失败后仍暂时
  // 保留 active_job_id；此时必须修复 pending 镜像，而不是把已知终态重新
  // 当成对账失败，否则 Composer 会永久保持“正在生成”。
  const normalizedPendingSnapshot = pendingSnapshot.active_job_id === activeJobId
    ? { ...pendingSnapshot, active_job_id: null }
    : pendingSnapshot;
  await refreshTerminalSession(
    apiPort,
    sessionId,
    workspaceId,
    sessionCacheKey,
    null,
    setState,
    normalizedPendingSnapshot,
    job,
  );
  return {
    jobStatus: job.status,
    lastEventCursor: options?.afterCursor ?? null,
    recoveredEventCount: 0,
  };
}

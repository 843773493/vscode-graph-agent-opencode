import { getJob, getSession } from "../api";
import { listPendingRequests } from "../pendingRequestsApi";
import {
  removePendingForTraceEvent,
  writePendingSnapshot,
} from "../state/conversations";
import {
  terminalStatusTextForEvent,
} from "../state/traceEvents";
import { replaceSessionMetadata } from "../state/session/sessions";
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
  if (job.status === "failed" || job.status === "timed_out") {
    return job.error_message
      ? `任务失败: ${job.error_message}`
      : `任务已终止: ${job.status}`;
  }
  if (job.status === "cancelled") {
    return "任务已取消";
  }
  return "任务已完成";
}

export async function refreshTerminalSession(
  apiPort: number,
  sessionId: string,
  workspaceId: string | null,
  sessionCacheKey: string,
  terminalTraceEvent: TraceEvent | null,
  refreshTurnDetails: (turnIds: string[]) => Promise<void>,
  setState: SetAppState,
  knownPendingSnapshot?: PendingRequestList,
  knownTerminalJob?: Job,
) {
  const terminalTurnId = terminalTraceEvent?.job_id ?? knownTerminalJob?.job_id;
  const [, updatedSession, pendingSnapshot] = await Promise.all([
    terminalTurnId
      ? refreshTurnDetails([terminalTurnId])
      : Promise.resolve(),
    getSession(apiPort, sessionId, workspaceId),
    knownPendingSnapshot
      ? Promise.resolve(knownPendingSnapshot)
      : listPendingRequests(apiPort, sessionId, workspaceId),
  ]);
  setState((latest) => {
    if (workspaceId && latest.currentSessionWorkspaceId !== workspaceId) {
      return latest;
    }
    const latestNext = replaceSessionMetadata(latest, updatedSession, workspaceId);
    latestNext.currentSessionWorkspaceId =
      workspaceId ?? latestNext.currentSessionWorkspaceId;
    if (terminalTraceEvent) {
      removePendingForTraceEvent(
        latestNext.pendingConversations,
        sessionCacheKey,
        terminalTraceEvent,
      );
    }
    writePendingSnapshot(
      latestNext.pendingConversations,
      latestNext.activeJobIdsBySession,
      pendingSnapshot,
      sessionCacheKey,
    );
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
      ? terminalStatusTextForEvent(terminalTraceEvent.type)
      : terminalStatusTextForJob(knownTerminalJob);
    return latestNext;
  });
}

export async function reconcileActiveJob(
  apiPort: number,
  sessionId: string,
  workspaceId: string | null,
  sessionCacheKey: string,
  activeJobId: string,
  refreshTurnDetails: (turnIds: string[]) => Promise<void>,
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
  if (pendingSnapshot.active_job_id === activeJobId) {
    throw new Error(
      `Job 已终止但会话仍标记为运行中: job_id=${activeJobId} status=${job.status}`,
    );
  }
  await refreshTerminalSession(
    apiPort,
    sessionId,
    workspaceId,
    sessionCacheKey,
    null,
    refreshTurnDetails,
    setState,
    pendingSnapshot,
    job,
  );
  return {
    jobStatus: job.status,
    lastEventCursor: options?.afterCursor ?? null,
    recoveredEventCount: 0,
  };
}

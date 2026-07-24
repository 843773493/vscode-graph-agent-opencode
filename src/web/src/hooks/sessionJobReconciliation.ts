import { getJob, getSession, listMessages } from "../api";
import { listPendingRequests } from "../pendingRequestsApi";
import { updateAttachmentSummariesFromMessages } from "../state/attachments";
import {
  removePendingForTraceEvent,
  writePendingSnapshot,
} from "../state/conversations";
import {
  isJobTerminalTraceType,
  terminalStatusTextForEvent,
} from "../state/traceEvents";
import { replaceSessionMetadata } from "../state/session/sessions";
import type {
  Job,
  JobStatus,
  PendingRequestList,
  TraceEvent,
} from "../types/backend";
import type { SetAppState } from "./contentViewLoaderTypes";
import { recoverTraceSnapshot } from "./sessionEventStreamRecovery";

const TERMINAL_JOB_STATUSES = new Set<JobStatus>([
  "completed",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
]);

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
  setState: SetAppState,
  knownPendingSnapshot?: PendingRequestList,
  knownTerminalJob?: Job,
) {
  const [messages, updatedSession, pendingSnapshot] = await Promise.all([
    listMessages(apiPort, sessionId, workspaceId),
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
    if (latest.currentSession?.session_id !== sessionId) {
      return latestNext;
    }
    latestNext.messages = messages.items;
    latestNext.messageHistoryNextCursor = messages.next_cursor ?? null;
    latestNext.messageHistoryHasMore = messages.has_more ?? false;
    latestNext.messageHistoryLoadingOlder = false;
    latestNext.messageHistoryError = null;
    updateAttachmentSummariesFromMessages(
      latestNext.sessionAttachmentSummaries,
      latestNext.messages,
    );
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
  setState: SetAppState,
) {
  const job = await getJob(apiPort, activeJobId, workspaceId);
  if (!TERMINAL_JOB_STATUSES.has(job.status)) {
    return;
  }

  const [pendingSnapshot, recovered] = await Promise.all([
    listPendingRequests(apiPort, sessionId, workspaceId),
    recoverTraceSnapshot(
      apiPort,
      sessionId,
      workspaceId,
      sessionCacheKey,
      setState,
      "已通过运行状态对账恢复事件历史",
    ),
  ]);
  if (pendingSnapshot.active_job_id === activeJobId) {
    throw new Error(
      `Job 已终止但会话仍标记为运行中: job_id=${activeJobId} status=${job.status}`,
    );
  }
  const terminalTraceEvent = [...recovered.traceEvents]
    .reverse()
    .find(
      (event) =>
        event.job_id === activeJobId && isJobTerminalTraceType(event.type),
    );
  await refreshTerminalSession(
    apiPort,
    sessionId,
    workspaceId,
    sessionCacheKey,
    terminalTraceEvent ?? null,
    setState,
    pendingSnapshot,
    job,
  );
}

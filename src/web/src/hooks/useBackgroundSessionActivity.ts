import { useEffect } from "react";
import { getJob } from "../api";
import { listPendingRequests } from "../pendingRequestsApi";
import { cloneMaps } from "../state/appStateMaps";
import { writePendingSnapshot } from "../state/conversations";
import {
  parseSessionScopeKey,
  sessionScopeKey,
} from "../state/session/sessionScope";
import type { AppState } from "../types/frontend";
import type { JobStatus } from "../types/backend";
import type { SetAppState } from "./contentViewLoaderTypes";
import { ACTIVE_JOB_RECONCILE_INTERVAL_MS } from "./sessionEventStreamPolicy";

const TERMINAL_JOB_STATUSES = new Set<JobStatus>([
  "completed",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
]);

function isActivelyViewed(state: AppState, sessionCacheKey: string): boolean {
  const sessionId = state.currentSession?.session_id;
  const workspaceId = state.currentSessionWorkspaceId;
  if (
    !sessionId
    || !workspaceId
    || sessionScopeKey(workspaceId, sessionId) !== sessionCacheKey
  ) {
    return false;
  }
  return document.visibilityState === "visible" && document.hasFocus();
}

export function useBackgroundSessionActivity({
  apiPort,
  activeJobIdsBySession,
  currentSessionCacheKey,
  setState,
}: {
  apiPort: number | null;
  activeJobIdsBySession: Map<string, string>;
  currentSessionCacheKey: string | null;
  setState: SetAppState;
}): void {
  useEffect(() => {
    if (!apiPort) {
      return;
    }
    const trackedJobs = [...activeJobIdsBySession.entries()].filter(
      ([sessionCacheKey]) => sessionCacheKey !== currentSessionCacheKey,
    );
    if (trackedJobs.length === 0) {
      return;
    }

    let cancelled = false;
    let reconciliationInFlight = false;
    const reconcile = async () => {
      if (cancelled || reconciliationInFlight) {
        return;
      }
      reconciliationInFlight = true;
      const results = await Promise.allSettled(
        trackedJobs.map(async ([sessionCacheKey, jobId]) => {
          const { workspaceId, sessionId } = parseSessionScopeKey(sessionCacheKey);
          const job = await getJob(apiPort, jobId, workspaceId);
          if (!TERMINAL_JOB_STATUSES.has(job.status)) {
            return;
          }
          const pendingSnapshot = await listPendingRequests(
            apiPort,
            sessionId,
            workspaceId,
          );
          if (cancelled) {
            return;
          }
          setState((previous) => {
            if (previous.activeJobIdsBySession.get(sessionCacheKey) !== jobId) {
              return previous;
            }
            const next = cloneMaps(previous);
            writePendingSnapshot(
              next.pendingConversations,
              next.activeJobIdsBySession,
              pendingSnapshot,
              sessionCacheKey,
            );
            if (
              !pendingSnapshot.active_job_id
              && (pendingSnapshot.requests?.length ?? 0) === 0
            ) {
              if (isActivelyViewed(previous, sessionCacheKey)) {
                next.unreadSessionKeys.delete(sessionCacheKey);
              } else {
                next.unreadSessionKeys.add(sessionCacheKey);
              }
            }
            return next;
          });
        }),
      );
      reconciliationInFlight = false;
      const failure = results.find(
        (result): result is PromiseRejectedResult => result.status === "rejected",
      );
      if (failure && !cancelled) {
        const message = failure.reason instanceof Error
          ? failure.reason.message
          : String(failure.reason);
        setState((previous) => ({
          ...previous,
          status: `后台会话活动状态对账失败: ${message}`,
        }));
      }
    };

    void reconcile();
    const intervalId = window.setInterval(
      () => void reconcile(),
      ACTIVE_JOB_RECONCILE_INTERVAL_MS,
    );
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [
    activeJobIdsBySession,
    apiPort,
    currentSessionCacheKey,
    setState,
  ]);
}

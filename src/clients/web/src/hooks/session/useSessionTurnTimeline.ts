import { useCallback, useEffect, type MutableRefObject } from "react";
import type { AppState } from "../../types/frontend";
import type { useSessionTurnHistory } from "../sessionTurnHistory/useSessionTurnHistory";
import type { SetAppState } from "../contentViewLoaderTypes";
import { errorMessage } from "../../utils/errorMessage";

/** getCurrentTurnTimeline 读 ref 取最新快照；currentTurnTimeline 读本次渲染的 state。 */
export function useSessionTurnTimeline(
  state: AppState,
  latestStateRef: MutableRefObject<AppState>,
  currentSessionCacheKey: string | null,
) {
  const getCurrentTurnTimeline = useCallback(() => {
    const latest = latestStateRef.current;
    if (!currentSessionCacheKey) return null;
    return latest.turnTimelinesBySession.get(currentSessionCacheKey) ?? null;
  }, [currentSessionCacheKey]);
  const currentTurnTimeline = currentSessionCacheKey
    ? state.turnTimelinesBySession.get(currentSessionCacheKey) ?? null
    : null;
  return { getCurrentTurnTimeline, currentTurnTimeline };
}

/** 活动 Job 读取权威 Turn metadata，终态读取完整详情；内部 Job 不生成用户 Turn。 */
export function useSessionJobTurnLoader({
  state,
  loadTurnDetails,
  latestStateRef,
  sessionCacheKey,
  refreshTurnHistory,
  setState,
}: {
  state: AppState;
  loadTurnDetails: ReturnType<typeof useSessionTurnHistory>["loadTurnDetails"];
  latestStateRef: MutableRefObject<AppState>;
  sessionCacheKey: string | null;
  refreshTurnHistory: () => void;
  setState: SetAppState;
}) {
  const activeJobId = sessionCacheKey
    ? state.activeJobIdsBySession.get(sessionCacheKey) : undefined;
  const activePending = sessionCacheKey && activeJobId
    ? state.pendingConversations.get(sessionCacheKey)?.find((conversation) =>
      conversation.jobId === activeJobId,
    ) : undefined;
  const activeTurnId = activePending?.turnId;
  const timeline = sessionCacheKey
    ? state.turnTimelinesBySession.get(sessionCacheKey) : undefined;
  const needsActiveTurn = Boolean(activeTurnId && timeline?.phase === "ready"
    && timeline.scopeKey === sessionCacheKey && !timeline.turnsById[activeTurnId]);
  useEffect(() => {
    if (!needsActiveTurn || !activeTurnId) return;
    let cancelled = false;
    // 普通准入不刷新整段历史，复用详情 reader 的 generation/epoch/abort 边界。
    void loadTurnDetails([activeTurnId], `active-turn:${activeTurnId}`, false, ["metadata"])
      .catch((error: unknown) => {
        if (cancelled) return;
        setState((previous) => ({
          ...previous,
          status: `加载活动 Turn 身份失败: ${errorMessage(error)}`,
        }));
      });
    return () => { cancelled = true; };
  }, [activeTurnId, loadTurnDetails, needsActiveTurn, sessionCacheKey, setState]);
  return useCallback(
    async (jobId: string) => {
      if (!sessionCacheKey) return;
      const state = latestStateRef.current;
      const pending = state.pendingConversations.get(sessionCacheKey)?.find((conversation) => conversation.jobId === jobId);
      const timeline = state.turnTimelinesBySession.get(sessionCacheKey);
      const turn = Object.values(timeline?.turnsById ?? {}).find((candidate) => candidate.job_id === jobId);
      const turnId = pending?.turnId ?? turn?.turn_id;
      if (!turnId) {
        refreshTurnHistory();
        return;
      }
      await loadTurnDetails([turnId], `terminal-turn:${turnId}`, true);
    },
    [loadTurnDetails, latestStateRef, sessionCacheKey, refreshTurnHistory],
  );
}

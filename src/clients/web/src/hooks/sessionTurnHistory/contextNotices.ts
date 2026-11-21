import { useCallback, type MutableRefObject } from "react";
import { loadSessionHistory, StaleTurnCursorHttpError } from "../../api/session/sessionTurnHistory";
import { applyContextNoticePage, writeTurnTimelineCache, type SessionTurnTimeline } from "../../state/session/turnTimeline";
import type { SetAppState } from "../contentViewLoaderTypes";

/** 显式动作只加载原页面剩余元数据，不推进 Turn 分页或自动排空来源。 */
export function useContextNoticeLoader({ apiPort, sessionId, threadId, workspaceId, sessionCacheKey,
  generationRef, requestSignal, getCurrentTimeline, setState, refreshTurnHistory,
}: {
  apiPort: number | null; sessionId: string | null; threadId?: string; workspaceId: string | null;
  sessionCacheKey: string | null; generationRef: MutableRefObject<number>; requestSignal: AbortSignal;
  getCurrentTimeline: () => SessionTurnTimeline | null; setState: SetAppState; refreshTurnHistory: () => void;
}): (cursor: string) => Promise<void> {
  return useCallback(async (cursor: string) => {
    const timeline = getCurrentTimeline();
    if (apiPort === null || !sessionId || !sessionCacheKey || !timeline?.threadId) {
      throw new Error("当前线程历史尚未就绪，无法加载上下文来源");
    }
    const generation = generationRef.current;
    try {
      const page = await loadSessionHistory(apiPort, sessionId, { context_cursor: cursor },
        workspaceId, requestSignal, timeline.threadId);
      requestSignal.throwIfAborted();
      setState(previous => {
        const current = previous.turnTimelinesBySession.get(sessionCacheKey);
        if (!current || current.generation !== generation || generationRef.current !== generation) return previous;
        return { ...previous, turnTimelinesBySession: writeTurnTimelineCache(previous.turnTimelinesBySession,
          sessionCacheKey, applyContextNoticePage(current, page, cursor)) };
      });
    } catch (error) {
      if (!requestSignal.aborted && generationRef.current === generation && error instanceof StaleTurnCursorHttpError) {
        refreshTurnHistory();
      }
      throw error;
    }
  }, [apiPort, sessionId, threadId, workspaceId, sessionCacheKey, generationRef, requestSignal,
    getCurrentTimeline, setState, refreshTurnHistory]);
}

import { useCallback, type MutableRefObject } from "react";
import type { AppState } from "../../types/frontend";
import type { useSessionTurnHistory } from "../sessionTurnHistory/useSessionTurnHistory";

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

/** Turn 完成事件入口，依赖 useSessionTurnHistory 的 loadTurnDetails，必须在其后调用。 */
export function useTerminalTurnLoader(
  loadTurnDetails: ReturnType<typeof useSessionTurnHistory>["loadTurnDetails"],
) {
  return useCallback(
    (turnId: string) => loadTurnDetails([turnId], `terminal-turn:${turnId}`, true),
    [loadTurnDetails],
  );
}

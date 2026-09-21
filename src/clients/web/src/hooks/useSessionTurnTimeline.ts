import { useCallback, type MutableRefObject } from "react";
import type { TurnHistoryInclude } from "../api/sessionTurnHistory";
import type { SessionTurnTimeline } from "../state/session/turnTimeline";
import type { AppState } from "../types/frontend";

/**
 * 会话 Turn 时间线垂直链路。
 *
 * 这条链路在 AppProvider 内存在函数体级循环依赖：useSessionTurnHistory 需要
 * getCurrentTimeline，而 Turn 完成事件要用的 loadTerminalTurn 又需要
 * useSessionTurnHistory 产出的 loadTurnDetails。这里按依赖方向拆成两个 hook，
 * 由 AppProvider 把 useSessionTurnTimeline 放在 useSessionTurnHistory 之前、
 * useTerminalTurnLoader 放在其后，从而在不引入 ref 中转的前提下解环，
 * 并保持 loadTerminalTurn 的依赖项与原实现完全一致。
 */
export function useSessionTurnTimeline({
  latestStateRef,
  currentSessionCacheKey,
}: {
  latestStateRef: MutableRefObject<AppState>;
  currentSessionCacheKey: string | null;
}) {
  const getCurrentTurnTimeline = useCallback((): SessionTurnTimeline | null => {
    const latest = latestStateRef.current;
    if (!currentSessionCacheKey) return null;
    return latest.turnTimelinesBySession.get(currentSessionCacheKey) ?? null;
  }, [currentSessionCacheKey]);
  // latestStateRef 在 AppProvider 每次渲染时都已同步为本次渲染的 state，
  // 因此这里读到的快照与直接从 state 读取逐字一致。
  const currentTurnTimeline = currentSessionCacheKey
    ? latestStateRef.current.turnTimelinesBySession.get(currentSessionCacheKey) ?? null
    : null;
  return { getCurrentTurnTimeline, currentTurnTimeline };
}

/** Turn 完成事件触发的详情加载入口。必须在 useSessionTurnHistory 之后调用。 */
export function useTerminalTurnLoader({
  loadTurnDetails,
}: {
  loadTurnDetails: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
    include?: TurnHistoryInclude[],
    toolCallIds?: string[],
  ) => Promise<void>;
}) {
  return useCallback(
    (turnId: string) => loadTurnDetails(
      [turnId],
      `terminal-turn:${turnId}`,
      true,
    ),
    [loadTurnDetails],
  );
}

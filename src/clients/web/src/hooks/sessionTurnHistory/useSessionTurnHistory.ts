import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { SetAppState } from "../contentViewLoaderTypes";
import type { TurnHistoryInclude } from "../../api/sessionTurnHistory";
import {
  dropTurn,
  writeTurnTimelineCache,
  type SessionTurnTimeline,
} from "../../state/session/turnTimeline";
import { useTurnBootstrap } from "./bootstrap";
import { useTurnDetailLoader } from "./details";
import {
  useAroundTurnLoader,
  useInitialTurnLoader,
  useNewerTurnLoader,
  useOlderTurnLoader,
} from "./page";

export function useSessionTurnHistory({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  getCurrentTimeline,
  reloadNonce,
  setState,
}: {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  getCurrentTimeline: () => SessionTurnTimeline | null;
  reloadNonce: number;
  setState: SetAppState;
}) {
  const generationRef = useRef(0);
  const invalidatedTurnIdsRef = useRef(new Set<string>());
  const [manualReloadNonce, setManualReloadNonce] = useState(0);
  const refreshTurnHistory = useCallback((missingTurnIds: string[] = []) => {
    if (missingTurnIds.length > 0 && sessionCacheKey) {
      for (const turnId of missingTurnIds) {
        invalidatedTurnIdsRef.current.add(turnId);
      }
      setState((previous) => {
        const current = previous.turnTimelinesBySession.get(sessionCacheKey);
        if (!current) return previous;
        let next = current;
        for (const turnId of missingTurnIds) {
          next = dropTurn(next, turnId);
        }
        return {
          ...previous,
          turnTimelinesBySession: writeTurnTimelineCache(
            previous.turnTimelinesBySession,
            sessionCacheKey,
            next,
          ),
          status: "历史已更新，已移除不属于当前上下文的旧 Turn",
        };
      });
    }
    setManualReloadNonce((nonce) => nonce + 1);
  }, [sessionCacheKey, setState]);
  const requestScopeController = useMemo(
    () => new AbortController(),
    [
      apiPort,
      manualReloadNonce,
      reloadNonce,
      sessionCacheKey,
      sessionId,
      workspaceId,
    ],
  );
  useEffect(
    () => () => requestScopeController.abort(),
    [requestScopeController],
  );
  const loadTurnDetailsRaw = useTurnDetailLoader({
    apiPort,
    sessionId,
    workspaceId,
    sessionCacheKey,
    generationRef,
    requestSignal: requestScopeController.signal,
    setState,
    onMissingTurn: refreshTurnHistory,
  });
  const loadTurnDetails = useCallback(async (
    turnIds: string[],
    requestIdentity: string | null = null,
    refreshAfterInFlight = false,
    include?: TurnHistoryInclude[],
    toolCallIds?: string[],
  ): Promise<void> => {
    const loadableTurnIds = turnIds.filter(
      (turnId) => !invalidatedTurnIdsRef.current.has(turnId),
    );
    if (loadableTurnIds.length === 0) return;
    await loadTurnDetailsRaw(
      loadableTurnIds,
      requestIdentity,
      refreshAfterInFlight,
      include,
      toolCallIds,
    );
  }, [loadTurnDetailsRaw]);
  const loadInitialTurns = useInitialTurnLoader({
    apiPort,
    sessionId,
    workspaceId,
    sessionCacheKey,
    generationRef,
    requestSignal: requestScopeController.signal,
    setState,
    onMissingTurn: refreshTurnHistory,
  });
  useTurnBootstrap({
    apiPort,
    sessionId,
    workspaceId,
    sessionCacheKey,
    reloadNonce,
    manualReloadNonce,
    generationRef,
    invalidatedTurnIdsRef,
    setState,
    loadInitialTurns,
  });
  const loadOlderTurns = useOlderTurnLoader({
    apiPort,
    sessionId,
    workspaceId,
    sessionCacheKey,
    getCurrentTimeline,
    generationRef,
    requestSignal: requestScopeController.signal,
    setState,
  });
  const loadNewerTurns = useNewerTurnLoader({
    apiPort,
    sessionId,
    workspaceId,
    sessionCacheKey,
    getCurrentTimeline,
    generationRef,
    requestSignal: requestScopeController.signal,
    setState,
  });
  const loadAroundTurn = useAroundTurnLoader({
    apiPort,
    sessionId,
    workspaceId,
    sessionCacheKey,
    getCurrentTimeline,
    generationRef,
    requestSignal: requestScopeController.signal,
    setState,
  });
  return {
    loadAroundTurn,
    loadNewerTurns,
    loadOlderTurns,
    loadTurnDetails,
    refreshTurnHistory,
  };
}

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { SetAppState } from "../contentViewLoaderTypes";
import { useTurnBootstrap } from "./bootstrap";
import { useTurnDetailLoader } from "./details";
import { useOlderTurnLoader } from "./page";

export function useSessionTurnHistory({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  reloadNonce,
  setState,
}: {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  reloadNonce: number;
  setState: SetAppState;
}) {
  const generationRef = useRef(0);
  const [manualReloadNonce, setManualReloadNonce] = useState(0);
  const refreshTurnHistory = useCallback(() => {
    setManualReloadNonce((nonce) => nonce + 1);
  }, []);
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
  const loadTurnDetails = useTurnDetailLoader({
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
    setState,
    loadTurnDetails,
  });
  const loadOlderTurns = useOlderTurnLoader({
    apiPort,
    sessionId,
    workspaceId,
    sessionCacheKey,
    generationRef,
    requestSignal: requestScopeController.signal,
    setState,
  });
  return { loadOlderTurns, loadTurnDetails, refreshTurnHistory };
}

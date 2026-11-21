import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { SetAppState } from "../contentViewLoaderTypes";
import type { TurnHistoryInclude } from "../../api/session/sessionTurnHistory";
import { loadSessionHistory } from "../../api/session/sessionTurnHistory";
import type { SessionTurnBootstrap } from "../../types/backend";
import {
  dropTurn,
  applyTurnHistoryPage,
  writeTurnTimelineCache,
  type SessionTurnTimeline,
} from "../../state/session/turnTimeline";
import type { ExecutionItemReference } from "../../state/session/internalExecutionHistory";
import { useTurnBootstrap } from "./bootstrap";
import { useTurnDetailLoader } from "./details";
import { useContextNoticeLoader } from "./contextNotices";
import {
  useAroundTurnLoader,
  useInitialTurnLoader,
  useNewerTurnLoader,
  useOlderTurnLoader,
} from "./page";

export function useSessionTurnHistory({
  initialBootstrap,
  apiPort,
  sessionId,
  threadId,
  workspaceId,
  sessionCacheKey,
  getCurrentTimeline,
  reloadNonce,
  setState,
}: {
  initialBootstrap?: Promise<SessionTurnBootstrap>;
  apiPort: number | null;
  sessionId: string | null;
  threadId?: string;
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
      threadId,
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
    threadId,
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
  const executionLoads = useRef(new Map<string, Promise<void>>());
  const executionBodyWindow = useRef<readonly string[]>([]);
  const retainTurnBodies = useCallback(async (turnIds: string[], executionItems: ExecutionItemReference[] = []) => {
    if (!sessionCacheKey) return;
    const timeline = getCurrentTimeline();
    const missing = turnIds.filter(id => timeline?.turnsById[id]?.bodyEvicted);
    const itemIds = executionItems.map(item => item.itemId);
    executionBodyWindow.current = itemIds;
    setState(previous => {
      const current = previous.turnTimelinesBySession.get(sessionCacheKey);
      if (!current) return previous;
      const retained = current.bodyWindowTurnIds;
      const retainedItems = current.bodyWindowExecutionItemIds;
      if (retained?.length === turnIds.length && retained.every((id, index) => id === turnIds[index])
        && retainedItems?.length === itemIds.length && retainedItems.every((id, index) => id === itemIds[index])) return previous;
      return { ...previous, turnTimelinesBySession: writeTurnTimelineCache(previous.turnTimelinesBySession,
        sessionCacheKey, { ...current, bodyWindowTurnIds: turnIds, bodyWindowExecutionItemIds: itemIds }) };
    });
    // 沿唯一详情 reader 分批补读，不生成分页游标，也不重载整个时间线。
    for (let index = 0; index < missing.length; index += 4) {
      if (requestScopeController.signal.aborted) return;
      await loadTurnDetails(missing.slice(index, index + 4));
    }
    if (apiPort === null || !sessionId || !workspaceId) return;
    const byExecution = new Map<string, string[]>();
    for (const reference of executionItems) {
      const execution = timeline?.internalExecutions.find(item => item.execution_id === reference.executionId);
      if (!execution?.display_parts.some(part => part.item_id === reference.itemId && part.bodyEvicted)
        && !execution?.response_parts.some(part => part.source.item_id === reference.itemId && part.bodyEvicted)) continue;
      const ids = byExecution.get(reference.executionId) ?? [];
      if (!ids.includes(reference.itemId)) ids.push(reference.itemId);
      byExecution.set(reference.executionId, ids);
    }
    const generation = generationRef.current;
    for (const [executionId, ids] of byExecution) for (let index = 0; index < ids.length; index += 4) {
      if (requestScopeController.signal.aborted) return;
      const selected = ids.slice(index, index + 4).filter(id => executionBodyWindow.current.includes(id));
      if (!selected.length) continue;
      const key = JSON.stringify([generation, executionId, selected]);
      let request = executionLoads.current.get(key);
      if (!request) {
        request = (async () => {
          const page = await loadSessionHistory(apiPort, sessionId, { execution_id: executionId,
            execution_item_ids: selected, include: ["internal", "assistant", "thinking", "tool_call", "tool_result"] },
            workspaceId, requestScopeController.signal, threadId);
          if (requestScopeController.signal.aborted || generation !== generationRef.current) return;
          setState(previous => {
            const current = previous.turnTimelinesBySession.get(sessionCacheKey);
            if (!current || current.generation !== generation) return previous;
            return { ...previous, turnTimelinesBySession: writeTurnTimelineCache(previous.turnTimelinesBySession,
              sessionCacheKey, applyTurnHistoryPage(current, page, "details")) };
          });
        })();
        executionLoads.current.set(key, request);
      }
      try { await request; }
      catch (error) { if (!requestScopeController.signal.aborted) throw error; }
      finally { executionLoads.current.delete(key); }
    }
  }, [apiPort, getCurrentTimeline, loadTurnDetails, requestScopeController, sessionCacheKey, sessionId, setState, threadId, workspaceId]);
  const loadInitialTurns = useInitialTurnLoader({
    apiPort,
    sessionId,
    threadId,
    workspaceId,
    sessionCacheKey,
    generationRef,
    requestSignal: requestScopeController.signal,
    setState,
    onMissingTurn: refreshTurnHistory,
  });
  useTurnBootstrap({
    initialBootstrap,
    apiPort,
    sessionId,
    threadId,
    workspaceId,
    sessionCacheKey,
    reloadNonce,
    manualReloadNonce,
    getCurrentTimeline,
    generationRef,
    invalidatedTurnIdsRef,
    setState,
    loadInitialTurns,
  });
  const loadOlderTurns = useOlderTurnLoader({
    apiPort,
    sessionId,
    threadId,
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
    threadId,
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
    threadId,
    workspaceId,
    sessionCacheKey,
    getCurrentTimeline,
    generationRef,
    requestSignal: requestScopeController.signal,
    setState,
  });
  const loadMoreContext = useContextNoticeLoader({ apiPort, sessionId, threadId, workspaceId, sessionCacheKey,
    getCurrentTimeline, generationRef, requestSignal: requestScopeController.signal, setState, refreshTurnHistory });
  return {
    retainTurnBodies,
    loadMoreContext,
    loadAroundTurn,
    loadNewerTurns,
    loadLatestTurns: loadInitialTurns,
    loadOlderTurns,
    loadTurnDetails,
    refreshTurnHistory,
  };
}

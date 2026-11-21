import { useCallback, useEffect, useMemo, useRef } from "react";
import { createSnapshotReadCoordinator, SnapshotReadSupersededError } from "../../../../shared/core/syncState";
import { listThreadPendingRequests } from "../../pendingRequestsApi";
import { writePendingSnapshot } from "../../state/conversations";
import { cloneMaps } from "../../state/appStateMaps";
import { errorMessage } from "../../utils/errorMessage";
import type { SetAppState } from "../contentViewLoaderTypes";

/** 执行队列的确认独立于 Goal；只在初始化、可见性恢复和原事件流恢复时读取。 */
export function useSessionExecutionSnapshot({ active, apiPort, currentSessionId, currentThreadId,
  currentSessionCacheKey, currentWorkspaceId, setState }: {
  active: boolean; apiPort: number; currentSessionId: string | null; currentThreadId: string | null;
  currentSessionCacheKey: string | null; currentWorkspaceId: string | null; setState: SetAppState;
}) {
  const reads = useRef(createSnapshotReadCoordinator());
  const owner = useMemo(() => ({}), [apiPort, currentSessionId, currentThreadId, currentWorkspaceId, currentSessionCacheKey]);
  const ownerRef = useRef(owner); ownerRef.current = owner;
  const activeRef = useRef(active); activeRef.current = active;
  const currentThreadIdRef = useRef(currentThreadId); currentThreadIdRef.current = currentThreadId;
  const currentSessionCacheKeyRef = useRef(currentSessionCacheKey); currentSessionCacheKeyRef.current = currentSessionCacheKey;
  const confirmExecutionSnapshot = useCallback(async () => {
    if (!activeRef.current || ownerRef.current !== owner || !currentSessionId || !currentThreadId || !currentWorkspaceId || !currentSessionCacheKey) return;
    const target = { sessionId: currentSessionId, threadId: currentThreadId, workspaceId: currentWorkspaceId,
      cacheKey: currentSessionCacheKey };
    const executionKey = `${apiPort}:${target.workspaceId}:${target.sessionId}:${target.threadId}`;
    try {
      const { snapshot, signal } = await reads.current.read(executionKey, async (signal) => ({
        snapshot: await listThreadPendingRequests(apiPort, target.sessionId, target.workspaceId, signal, target.threadId), signal,
      }));
      setState((previous) => {
        if (signal.aborted || ownerRef.current !== owner || !activeRef.current || previous.currentSession?.session_id !== target.sessionId
          || previous.currentSessionWorkspaceId !== target.workspaceId
          || currentThreadIdRef.current !== target.threadId
          || currentSessionCacheKeyRef.current !== target.cacheKey) return previous;
        const next = cloneMaps(previous);
        writePendingSnapshot(
          next.pendingConversations, next.activeJobIdsBySession, snapshot,
          target.cacheKey, next.pendingQueueVersions,
        );
        next.sessionExecutionSyncError = null;
        return next;
      });
    } catch (error) {
      if (error instanceof SnapshotReadSupersededError) return;
      setState((previous) => ownerRef.current !== owner || !activeRef.current || previous.currentSession?.session_id !== target.sessionId
        || previous.currentSessionWorkspaceId !== target.workspaceId
        || currentThreadIdRef.current !== target.threadId
        || currentSessionCacheKeyRef.current !== target.cacheKey
        ? previous : { ...previous, sessionExecutionSyncError: errorMessage(error) });
    }
  }, [apiPort, currentSessionCacheKey, currentSessionId, currentThreadId, currentWorkspaceId, owner, setState]);

  const confirmSessionState = useCallback(async (afterSubscription = false) => {
    if (!activeRef.current || document.visibilityState === "hidden") return;
    if (afterSubscription) reads.current.dispose();
    await confirmExecutionSnapshot();
  }, [confirmExecutionSnapshot]);
  useEffect(() => {
    if (!active) return;
    const confirm = () => {
      if (document.visibilityState === "hidden") reads.current.dispose();
      else void confirmSessionState();
    };
    confirm();
    document.addEventListener("visibilitychange", confirm);
    return () => { reads.current.dispose(); document.removeEventListener("visibilitychange", confirm); };
  }, [active, confirmSessionState]);
  return { confirmExecutionSnapshot, confirmSessionState };
}

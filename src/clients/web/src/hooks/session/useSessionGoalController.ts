import { useCallback, useEffect, useRef } from "react";
import {
  clearSessionGoal as apiClearSessionGoal,
  getSessionGoal as apiGetSessionGoal,
  getSessionGoalOperation as apiGetSessionGoalOperation,
  updateSessionGoal as apiUpdateSessionGoal,
} from "../../api";
import type { SessionGoal, SessionGoalOperationResultDTO, SessionGoalUpdateRequest } from "../../types/backend";
import type { SetAppState } from "../contentViewLoaderTypes";
import { errorMessage } from "../../utils/errorMessage";
import { assertGoalOwner } from "../../protocol/sessionGoal";
import { listThreadPendingRequests } from "../../pendingRequestsApi";
import { writePendingSnapshot } from "../../state/conversations";
import { cloneMaps } from "../../state/appStateMaps";
import { applyVersionedValue, createSerialOperationQueue, createSnapshotReadCoordinator, SnapshotReadSupersededError } from "../../../../shared/core/syncState";

interface GoalTarget {
  sessionId: string;
  threadId: string;
  workspaceId: string | null;
}

const GOAL_CONFIRM_INTERVAL_MS = 30_000;

/** Goal 读取和控制操作共用精确 owner 键。 */
function goalRequestKey(apiPort: number, target: GoalTarget): string {
  return `${apiPort}:${target.workspaceId ?? ""}:${target.sessionId}`;
}

export function useSessionGoalController({
  active = true,
  apiPort,
  currentSessionId,
  currentMainThreadId,
  currentGoalControlRevision,
  currentGoalSyncVersion,
  currentThreadId,
  currentSessionCacheKey,
  currentWorkspaceId,
  setState,
}: {
  active?: boolean;
  apiPort: number;
  currentSessionId: string | null;
  currentMainThreadId: string | null;
  currentGoalControlRevision: number;
  currentGoalSyncVersion: number;
  currentThreadId: string | null;
  currentSessionCacheKey: string | null;
  currentWorkspaceId: string | null;
  setState: SetAppState;
}) {
  const goalReadCoordinatorRef = useRef(createSnapshotReadCoordinator());
  const executionReadCoordinatorRef = useRef(createSnapshotReadCoordinator());
  const currentThreadIdRef = useRef(currentThreadId);
  currentThreadIdRef.current = currentThreadId;
  const currentGoalControlRevisionRef = useRef(currentGoalControlRevision);
  const currentGoalSyncVersionRef = useRef(currentGoalSyncVersion);
  const currentSessionCacheKeyRef = useRef(currentSessionCacheKey);
  currentSessionCacheKeyRef.current = currentSessionCacheKey;
  const goalWriteQueueRef = useRef(createSerialOperationQueue());
  const currentGoalKey = goalRequestKey(apiPort, {
    sessionId: currentSessionId ?? "", threadId: currentMainThreadId ?? "", workspaceId: currentWorkspaceId,
  });
  const currentGoalKeyRef = useRef(currentGoalKey);
  const ownerRef = useRef<object | null>({});
  if (currentGoalKeyRef.current !== currentGoalKey) {
    currentGoalKeyRef.current = currentGoalKey;
    ownerRef.current = {};
    currentGoalControlRevisionRef.current = 0;
    currentGoalSyncVersionRef.current = 0;
  } else if (currentGoalSyncVersion >= currentGoalSyncVersionRef.current) {
    currentGoalControlRevisionRef.current = currentGoalControlRevision;
    currentGoalSyncVersionRef.current = currentGoalSyncVersion;
  }
  const owner = ownerRef.current;
  const activeRef = useRef(active);
  activeRef.current = active;

  const confirmExecutionSnapshot = useCallback(async () => {
    if (!activeRef.current || ownerRef.current !== owner || !currentSessionId || !currentThreadId || !currentWorkspaceId || !currentSessionCacheKey) return;
    const target = { sessionId: currentSessionId, threadId: currentThreadId, workspaceId: currentWorkspaceId,
      cacheKey: currentSessionCacheKey };
    const executionKey = `${apiPort}:${target.workspaceId}:${target.sessionId}:${target.threadId}`;
    try {
      const { snapshot, signal } = await executionReadCoordinatorRef.current.read(executionKey, async (signal) => ({
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

  const refreshGoal = useCallback(async (
    target: GoalTarget = {
      sessionId: currentSessionId ?? "",
      threadId: currentMainThreadId ?? "",
      workspaceId: currentWorkspaceId,
    },
    options: { silent?: boolean } = {},
  ): Promise<SessionGoal | null> => {
    if (!activeRef.current || ownerRef.current !== owner) return null;
    if (!target.sessionId) {
      setState((previous) => ({
        ...previous,
        currentGoal: null,
        currentGoalSessionId: null,
        currentGoalControlRevision: 0,
        currentGoalSyncVersion: 0,
        goalLoading: false,
        goalError: null,
      }));
      return null;
    }
    const requestKey = goalRequestKey(apiPort, target);
    if (currentGoalKeyRef.current !== requestKey) return null;
    if (!options.silent) {
      setState((previous) => ownerRef.current !== owner ? previous : ({
        ...previous,
        goalLoading: true,
        goalError: null,
      }));
    }
    const request = goalReadCoordinatorRef.current.read(requestKey, async (signal): Promise<SessionGoal | null> => {
      try {
        const snapshot = await apiGetSessionGoal(
          apiPort,
          target.sessionId,
          target.workspaceId,
          signal,
          currentGoalSyncVersionRef.current,
        );
        assertGoalOwner(snapshot, { session_id: target.sessionId, thread_id: target.threadId });
        const goal = snapshot.goal ?? null;
        if (goal) assertGoalOwner(goal, { session_id: target.sessionId, thread_id: target.threadId });
        setState((previous) => {
          if (signal.aborted || ownerRef.current !== owner || !activeRef.current || currentGoalKeyRef.current !== requestKey) return previous;
          if (previous.currentSession?.session_id !== target.sessionId) {
            return previous;
          }
          if (previous.currentSessionWorkspaceId !== target.workspaceId) return previous;
          if (snapshot.sync_version < previous.currentGoalSyncVersion) return previous;
          currentGoalControlRevisionRef.current = snapshot.control_revision;
          currentGoalSyncVersionRef.current = snapshot.sync_version;
          return {
            ...previous,
            currentGoal: goal === null ? null : applyVersionedValue(
              previous.currentGoal,
              goal,
              (item) => item.sync_version,
            ),
            currentGoalSessionId: target.sessionId,
            currentGoalControlRevision: snapshot.control_revision,
            currentGoalSyncVersion: snapshot.sync_version,
            goalLoading: false,
            goalError: null,
          };
        });
        return goal;
      } catch (error) {
        if (signal.aborted || error instanceof SnapshotReadSupersededError) return null;
        const message = errorMessage(error);
        setState((previous) => {
          if (signal.aborted || ownerRef.current !== owner || !activeRef.current || currentGoalKeyRef.current !== requestKey) return previous;
          if (previous.currentSession?.session_id !== target.sessionId
            || previous.currentSessionWorkspaceId !== target.workspaceId) {
            return previous;
          }
          return {
            ...previous,
            goalLoading: false,
            goalError: message,
          };
        });
        throw error;
      }
    }).catch((error: unknown) => {
      if (error instanceof SnapshotReadSupersededError) return null;
      throw error;
    });
    return request;
  }, [apiPort, currentSessionId, currentMainThreadId, currentWorkspaceId, owner, setState]);

  const reconcileAfterFailure = useCallback(async (
    target: GoalTarget,
    operationError: unknown,
  ): Promise<never> => {
    const operationMessage = errorMessage(operationError);
    try {
      await refreshGoal(target, { silent: true });
    } catch (refreshError) {
      throw new Error(
        `${operationMessage}；重新读取 Goal 也失败：${errorMessage(refreshError)}`,
      );
    }
    throw operationError;
  }, [refreshGoal]);

  const reconcileUnknown = useCallback(async (
    target: GoalTarget,
    operationId: string,
  ): Promise<SessionGoalOperationResultDTO | null> => {
    const result = await apiGetSessionGoalOperation(
      apiPort, target.sessionId, operationId, target.workspaceId,
    );
    assertGoalOwner(result, { session_id: target.sessionId, thread_id: target.threadId });
    return result.status === "unknown" ? null : result;
  }, [apiPort]);

  const updateGoal = useCallback(async (
    payload: SessionGoalUpdateRequest,
    target: GoalTarget = {
      sessionId: currentSessionId ?? "",
      threadId: currentMainThreadId ?? "",
      workspaceId: currentWorkspaceId,
    },
  ): Promise<SessionGoal> => {
    if (!target.sessionId) {
      throw new Error("当前没有可设置 Goal 的会话");
    }
    const requestKey = goalRequestKey(apiPort, target);
    const frozenPayload = { ...payload };
    return goalWriteQueueRef.current.run(requestKey, async () => {
      if (ownerRef.current !== owner || !activeRef.current || currentGoalKeyRef.current !== requestKey) throw new SnapshotReadSupersededError();
      goalReadCoordinatorRef.current.invalidate(requestKey);
      setState((previous) => ownerRef.current === owner && currentGoalKeyRef.current === requestKey
        ? { ...previous, goalLoading: true, goalError: null } : previous);
      const expectedControlRevision = frozenPayload.expected_control_revision
        ?? currentGoalControlRevisionRef.current;
      const operationId = crypto.randomUUID();
      let result: SessionGoalOperationResultDTO;
      try {
        result = await apiUpdateSessionGoal(apiPort, target.sessionId, {
          ...frozenPayload,
          operation_id: operationId,
          expected_control_revision: expectedControlRevision,
        }, target.workspaceId);
      } catch (operationError) {
        let known: SessionGoalOperationResultDTO | null;
        try { known = await reconcileUnknown(target, operationId); }
        catch (queryError) {
          return reconcileAfterFailure(target, new Error(
            `${errorMessage(operationError)}；查询 Goal 原操作回执失败：${errorMessage(queryError)}`,
          ));
        }
        if (known === null) return reconcileAfterFailure(target, operationError);
        result = known;
      }
      assertGoalOwner(result, { session_id: target.sessionId, thread_id: target.threadId });
      if (result.status !== "applied" || !result.goal) {
        await refreshGoal(target, { silent: true });
        throw new Error(`Goal 操作未应用: ${result.status}`);
      }
      const goal = result.goal;
      goalReadCoordinatorRef.current.invalidate(requestKey);
      if (ownerRef.current === owner && currentGoalKeyRef.current === requestKey && result.sync_version >= currentGoalSyncVersionRef.current) {
        currentGoalControlRevisionRef.current = result.control_revision;
        currentGoalSyncVersionRef.current = result.sync_version;
      }
      setState((previous) => {
        if (
          ownerRef.current !== owner || currentGoalKeyRef.current !== requestKey
          || previous.currentSession?.session_id !== target.sessionId
          || previous.currentSessionWorkspaceId !== target.workspaceId
          || result.sync_version < previous.currentGoalSyncVersion
        ) {
          return previous;
        }
        return {
          ...previous,
          currentGoal: goal,
          currentGoalSessionId: target.sessionId,
          currentGoalControlRevision: result.control_revision,
          currentGoalSyncVersion: result.sync_version,
          goalLoading: false,
          goalError: null,
        };
      });
      return goal;
    });
  }, [
    apiPort,
    currentSessionId,
    currentMainThreadId,
    currentWorkspaceId,
    owner,
    reconcileUnknown,
    reconcileAfterFailure,
    refreshGoal,
    setState,
  ]);

  const clearGoal = useCallback(async (
    target: GoalTarget = {
      sessionId: currentSessionId ?? "",
      threadId: currentMainThreadId ?? "",
      workspaceId: currentWorkspaceId,
    },
    expectedControlRevision?: number,
  ): Promise<void> => {
    if (!target.sessionId) {
      throw new Error("当前没有可清除 Goal 的会话");
    }
    const requestKey = goalRequestKey(apiPort, target);
    return goalWriteQueueRef.current.run(requestKey, async () => {
      if (ownerRef.current !== owner || !activeRef.current || currentGoalKeyRef.current !== requestKey) throw new SnapshotReadSupersededError();
      goalReadCoordinatorRef.current.invalidate(requestKey);
      setState((previous) => ownerRef.current === owner && currentGoalKeyRef.current === requestKey
        ? { ...previous, goalLoading: true, goalError: null } : previous);
      const operationId = crypto.randomUUID();
      let result: SessionGoalOperationResultDTO;
      try {
        result = await apiClearSessionGoal(apiPort, target.sessionId, {
          operation_id: operationId,
          expected_control_revision: expectedControlRevision ?? currentGoalControlRevisionRef.current,
        }, target.workspaceId);
      } catch (operationError) {
        let known: SessionGoalOperationResultDTO | null;
        try { known = await reconcileUnknown(target, operationId); }
        catch (queryError) {
          return reconcileAfterFailure(target, new Error(
            `${errorMessage(operationError)}；查询 Goal 原操作回执失败：${errorMessage(queryError)}`,
          ));
        }
        if (known === null) return reconcileAfterFailure(target, operationError);
        result = known;
      }
      assertGoalOwner(result, { session_id: target.sessionId, thread_id: target.threadId });
      if (result.status !== "applied" && result.status !== "already_missing") {
        await refreshGoal(target, { silent: true });
        throw new Error(`Goal 清除操作未应用: ${result.status}`);
      }
      goalReadCoordinatorRef.current.invalidate(requestKey);
      if (ownerRef.current === owner && currentGoalKeyRef.current === requestKey && result.sync_version >= currentGoalSyncVersionRef.current) {
        currentGoalControlRevisionRef.current = result.control_revision;
        currentGoalSyncVersionRef.current = result.sync_version;
      }
      setState((previous) => {
        if (
          ownerRef.current !== owner || currentGoalKeyRef.current !== requestKey
          || previous.currentSession?.session_id !== target.sessionId
          || previous.currentSessionWorkspaceId !== target.workspaceId
          || result.sync_version < previous.currentGoalSyncVersion
        ) {
          return previous;
        }
        return {
          ...previous,
          currentGoal: null,
          currentGoalSessionId: target.sessionId,
          currentGoalControlRevision: result.control_revision,
          currentGoalSyncVersion: result.sync_version,
          goalLoading: false,
          goalError: null,
        };
      });
    });
  }, [
    apiPort,
    currentSessionId,
    currentMainThreadId,
    currentWorkspaceId,
    owner,
    reconcileUnknown,
    reconcileAfterFailure,
    refreshGoal,
    setState,
  ]);

  useEffect(() => {
    ownerRef.current = owner;
    setState((previous) => ({
      ...previous,
      currentGoal: null,
      currentGoalSessionId: currentSessionId,
      currentGoalControlRevision: 0,
      currentGoalSyncVersion: 0,
      goalLoading: Boolean(currentSessionId),
      goalError: null,
    }));
    return () => {
      if (ownerRef.current === owner) ownerRef.current = null;
      goalReadCoordinatorRef.current.invalidate(goalRequestKey(apiPort, {
        sessionId: currentSessionId ?? "", threadId: currentMainThreadId ?? "",
        workspaceId: currentWorkspaceId,
      }));
    };
  }, [apiPort, currentSessionId, currentMainThreadId, currentWorkspaceId, owner, setState]);

  const confirmSessionState = useCallback(async (afterSubscription = false) => {
    if (!activeRef.current || document.visibilityState !== "visible") return;
    if (afterSubscription) {
      // 订阅前已发出的快照不能证明加载窗口内没有遗漏更新。
      goalReadCoordinatorRef.current.invalidate(currentGoalKey);
      executionReadCoordinatorRef.current.invalidate(`${apiPort}:${currentWorkspaceId}:${currentSessionId}:${currentThreadId}`);
    }
    await Promise.allSettled([refreshGoal(undefined, { silent: true }), confirmExecutionSnapshot()]);
  }, [apiPort, currentGoalKey, currentSessionId, currentThreadId, currentWorkspaceId, refreshGoal, confirmExecutionSnapshot]);

  useEffect(() => {
    if (!active || !currentSessionId || !currentThreadId || !currentSessionCacheKey) {
      return;
    }
    let intervalId: number | null = null;
    const startVisibleConfirmation = () => {
      if (document.visibilityState !== "visible") return;
      if (intervalId === null) {
        intervalId = window.setInterval(() => {
          void confirmSessionState();
        }, GOAL_CONFIRM_INTERVAL_MS);
      }
      void confirmSessionState();
    };
    const refreshWhenVisible = () => {
      if (document.visibilityState === "hidden") {
        if (intervalId !== null) window.clearInterval(intervalId);
        intervalId = null;
        goalReadCoordinatorRef.current.invalidate(currentGoalKey);
        executionReadCoordinatorRef.current.invalidate(
          `${apiPort}:${currentWorkspaceId}:${currentSessionId}:${currentThreadId}`,
        );
      } else {
        startVisibleConfirmation();
      }
    };
    window.addEventListener("focus", refreshWhenVisible);
    window.addEventListener("online", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    startVisibleConfirmation();
    return () => {
      if (intervalId !== null) window.clearInterval(intervalId);
      goalReadCoordinatorRef.current.invalidate(currentGoalKey);
      executionReadCoordinatorRef.current.invalidate(
        `${apiPort}:${currentWorkspaceId}:${currentSessionId}:${currentThreadId}`,
      );
      window.removeEventListener("focus", refreshWhenVisible);
      window.removeEventListener("online", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [active, apiPort, currentGoalKey, currentWorkspaceId, confirmSessionState, currentSessionCacheKey, currentSessionId, currentThreadId]);

  return { refreshGoal, updateGoal, clearGoal, confirmSessionState };
}

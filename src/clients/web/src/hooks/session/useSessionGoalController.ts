import { useCallback, useEffect, useRef } from "react";
import {
  clearSessionGoal as apiClearSessionGoal,
  getSessionGoal as apiGetSessionGoal,
  updateSessionGoal as apiUpdateSessionGoal,
} from "../../api";
import type { SessionGoal, SessionGoalUpdateRequest } from "../../types/backend";
import type { SetAppState } from "../contentViewLoaderTypes";
import { errorMessage } from "../../utils/errorMessage";
import { trackInFlightRequest } from "../runtime/inFlightRequests";
import { assertGoalOwner } from "../../state/sessionGoal";

interface GoalTarget {
  sessionId: string;
  threadId: string;
  workspaceId: string | null;
}

const SESSION_AUXILIARY_LOAD_DELAY_MS = 200;

/** 三条 Goal 链路（读取、设置、清除）共用的请求合并键与写入代际键。 */
function goalRequestKey(apiPort: number, target: GoalTarget): string {
  return `${apiPort}:${target.workspaceId ?? ""}:${target.sessionId}`;
}

export function useSessionGoalController({
  apiPort,
  currentSessionId,
  currentMainThreadId,
  currentWorkspaceId,
  setState,
}: {
  apiPort: number;
  currentSessionId: string | null;
  currentMainThreadId: string | null;
  currentWorkspaceId: string | null;
  setState: SetAppState;
}) {
  const inFlightGoalRequestsRef = useRef<Map<string, Promise<SessionGoal | null>>>(
    new Map(),
  );
  // 每条 Goal 请求键的写入代际：写入开始时递增，读取只采样当前代际、不抢占
  // 序列。写入前发出的读取若迟到，不能用旧快照覆盖新写入；读取在写入之后
  // 发出时仍可校准后端真值。这样同时避免旧设置复活已清除 Goal 和旧空值隐藏
  // 刚创建的 Goal。
  const goalWriteSeqRef = useRef<Map<string, number>>(new Map());

  const beginGoalWrite = useCallback((requestKey: string): number => {
    const next = (goalWriteSeqRef.current.get(requestKey) ?? 0) + 1;
    goalWriteSeqRef.current.set(requestKey, next);
    inFlightGoalRequestsRef.current.delete(requestKey);
    return next;
  }, []);

  const isLatestGoalWrite = useCallback(
    (requestKey: string, writeSeq: number): boolean =>
      goalWriteSeqRef.current.get(requestKey) === writeSeq,
    [],
  );

  const finishGoalWrite = useCallback((requestKey: string, writeSeq: number): number | null => {
    if (!isLatestGoalWrite(requestKey, writeSeq)) return null;
    const settledSeq = writeSeq + 1;
    goalWriteSeqRef.current.set(requestKey, settledSeq);
    inFlightGoalRequestsRef.current.delete(requestKey);
    return settledSeq;
  }, [isLatestGoalWrite]);

  const refreshGoal = useCallback(async (
    target: GoalTarget = {
      sessionId: currentSessionId ?? "",
      threadId: currentMainThreadId ?? "",
      workspaceId: currentWorkspaceId,
    },
    options: { silent?: boolean } = {},
  ): Promise<SessionGoal | null> => {
    if (!target.sessionId) {
      setState((previous) => ({
        ...previous,
        currentGoal: null,
        currentGoalSessionId: null,
        goalLoading: false,
        goalError: null,
      }));
      return null;
    }
    if (!options.silent) {
      setState((previous) => ({
        ...previous,
        goalLoading: true,
        goalError: null,
      }));
    }
    const requestKey = goalRequestKey(apiPort, target);
    const readWriteSeq = goalWriteSeqRef.current.get(requestKey) ?? 0;
    const inFlight = inFlightGoalRequestsRef.current.get(requestKey);
    if (inFlight) {
      return inFlight;
    }
    const request = (async (): Promise<SessionGoal | null> => {
      try {
        const goal = await apiGetSessionGoal(
          apiPort,
          target.sessionId,
          target.workspaceId,
        );
        if (goal) assertGoalOwner(goal, { session_id: target.sessionId, thread_id: target.threadId });
        setState((previous) => {
          if ((goalWriteSeqRef.current.get(requestKey) ?? 0) !== readWriteSeq) {
            return previous;
          }
          if (previous.currentSession?.session_id !== target.sessionId) {
            return previous;
          }
          if (previous.currentSessionWorkspaceId !== target.workspaceId) return previous;
          return {
            ...previous,
            currentGoal: goal,
            currentGoalSessionId: target.sessionId,
            goalLoading: false,
            goalError: null,
          };
        });
        return goal;
      } catch (error) {
        const message = errorMessage(error);
        setState((previous) => {
          if ((goalWriteSeqRef.current.get(requestKey) ?? 0) !== readWriteSeq) {
            return previous;
          }
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
    })();
    trackInFlightRequest(inFlightGoalRequestsRef.current, requestKey, request);
    return request;
  }, [apiPort, currentSessionId, currentMainThreadId, currentWorkspaceId, setState]);

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
    const writeSeq = beginGoalWrite(requestKey);
    setState((previous) => ({ ...previous, goalLoading: true, goalError: null }));
    try {
      const goal = await apiUpdateSessionGoal(
        apiPort,
        target.sessionId,
        payload,
        target.workspaceId,
      );
      assertGoalOwner(goal, { session_id: target.sessionId, thread_id: target.threadId });
      const settledSeq = finishGoalWrite(requestKey, writeSeq);
      setState((previous) => {
        if (
          settledSeq === null
          || !isLatestGoalWrite(requestKey, settledSeq)
          || previous.currentSession?.session_id !== target.sessionId
          || previous.currentSessionWorkspaceId !== target.workspaceId
        ) {
          return previous;
        }
        return {
          ...previous,
          currentGoal: goal,
          currentGoalSessionId: target.sessionId,
          goalLoading: false,
          goalError: null,
        };
      });
      return goal;
    } catch (error) {
      if (finishGoalWrite(requestKey, writeSeq) === null) throw error;
      return reconcileAfterFailure(target, error);
    }
  }, [
    apiPort,
    beginGoalWrite,
    currentSessionId,
    currentMainThreadId,
    currentWorkspaceId,
    finishGoalWrite,
    isLatestGoalWrite,
    reconcileAfterFailure,
    setState,
  ]);

  const clearGoal = useCallback(async (
    target: GoalTarget = {
      sessionId: currentSessionId ?? "",
      threadId: currentMainThreadId ?? "",
      workspaceId: currentWorkspaceId,
    },
  ): Promise<void> => {
    if (!target.sessionId) {
      throw new Error("当前没有可清除 Goal 的会话");
    }
    const requestKey = goalRequestKey(apiPort, target);
    const writeSeq = beginGoalWrite(requestKey);
    setState((previous) => ({ ...previous, goalLoading: true, goalError: null }));
    try {
      const result = await apiClearSessionGoal(apiPort, target.sessionId, target.workspaceId);
      assertGoalOwner(result, { session_id: target.sessionId, thread_id: target.threadId });
      const settledSeq = finishGoalWrite(requestKey, writeSeq);
      setState((previous) => {
        if (
          settledSeq === null
          || !isLatestGoalWrite(requestKey, settledSeq)
          || previous.currentSession?.session_id !== target.sessionId
          || previous.currentSessionWorkspaceId !== target.workspaceId
        ) {
          return previous;
        }
        return {
          ...previous,
          currentGoal: null,
          currentGoalSessionId: target.sessionId,
          goalLoading: false,
          goalError: null,
        };
      });
    } catch (error) {
      if (finishGoalWrite(requestKey, writeSeq) === null) throw error;
      await reconcileAfterFailure(target, error);
    }
  }, [
    apiPort,
    beginGoalWrite,
    currentSessionId,
    currentMainThreadId,
    currentWorkspaceId,
    finishGoalWrite,
    isLatestGoalWrite,
    reconcileAfterFailure,
    setState,
  ]);

  useEffect(() => {
    setState((previous) => ({
      ...previous,
      currentGoal: null,
      currentGoalSessionId: currentSessionId,
      goalLoading: Boolean(currentSessionId),
      goalError: null,
    }));
    if (!currentSessionId) {
      return;
    }
    const timerId = window.setTimeout(() => {
      void refreshGoal().catch(() => {
        // 请求错误已写入 AppState，界面必须直接呈现。
      });
    }, SESSION_AUXILIARY_LOAD_DELAY_MS);
    return () => window.clearTimeout(timerId);
  }, [currentSessionId, currentWorkspaceId, refreshGoal, setState]);

  useEffect(() => {
    if (!currentSessionId) {
      return;
    }
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") {
        void refreshGoal(undefined, { silent: true }).catch(() => {
          // 请求错误已写入 AppState，界面必须直接呈现。
        });
      }
    };
    window.addEventListener("focus", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.removeEventListener("focus", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [currentSessionId, refreshGoal]);

  return { refreshGoal, updateGoal, clearGoal };
}

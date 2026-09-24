import { useCallback, useEffect, useRef } from "react";
import {
  clearSessionGoal as apiClearSessionGoal,
  getSessionGoal as apiGetSessionGoal,
  updateSessionGoal as apiUpdateSessionGoal,
} from "../../api";
import type { SessionGoal, SessionGoalUpdateRequest } from "../../types/backend";
import type { SetAppState } from "../contentViewLoaderTypes";
import { errorMessage } from "../../utils/errorMessage";

interface GoalTarget {
  sessionId: string;
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
  currentWorkspaceId,
  setState,
}: {
  apiPort: number;
  currentSessionId: string | null;
  currentWorkspaceId: string | null;
    setState: SetAppState;
}) {
  const inFlightGoalRequestsRef = useRef<Map<string, Promise<SessionGoal | null>>>(
    new Map(),
  );
  // 每条 Goal 请求键的写入代际：只有序列里最新的那次写（设置/清除）才有权把
  // 结果写回 AppState。否则「先设置后清除」时，迟到的设置回包会把用户刚刚
  // 清除的 Goal 复活。读取不参与这条序列：读取是对后端真值的采样，若读取也
  // 抢占代际，一次并发的聚焦校准会把用户已完成的清除判定成过期结果。
  const goalWriteSeqRef = useRef<Map<string, number>>(new Map());

  const beginGoalWrite = useCallback((requestKey: string): number => {
    const next = (goalWriteSeqRef.current.get(requestKey) ?? 0) + 1;
    goalWriteSeqRef.current.set(requestKey, next);
    return next;
  }, []);

  const isLatestGoalWrite = useCallback(
    (requestKey: string, writeSeq: number): boolean =>
      goalWriteSeqRef.current.get(requestKey) === writeSeq,
    [],
  );

  const refreshGoal = useCallback(async (
    target: GoalTarget = {
      sessionId: currentSessionId ?? "",
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
        setState((previous) => {
          if (previous.currentSession?.session_id !== target.sessionId) {
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
        const message = errorMessage(error);
        setState((previous) => {
          if (previous.currentSession?.session_id !== target.sessionId) {
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
    inFlightGoalRequestsRef.current.set(requestKey, request);
    void request.then(() => {
      if (inFlightGoalRequestsRef.current.get(requestKey) === request) {
        inFlightGoalRequestsRef.current.delete(requestKey);
      }
    }, () => {
      if (inFlightGoalRequestsRef.current.get(requestKey) === request) {
        inFlightGoalRequestsRef.current.delete(requestKey);
      }
    });
    return request;
  }, [apiPort, currentSessionId, currentWorkspaceId, setState]);

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
      setState((previous) => {
        if (
          !isLatestGoalWrite(requestKey, writeSeq)
          || previous.currentSession?.session_id !== target.sessionId
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
      return reconcileAfterFailure(target, error);
    }
  }, [
    apiPort,
    beginGoalWrite,
    currentSessionId,
    currentWorkspaceId,
    isLatestGoalWrite,
    reconcileAfterFailure,
    setState,
  ]);

  const clearGoal = useCallback(async (
    target: GoalTarget = {
      sessionId: currentSessionId ?? "",
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
      await apiClearSessionGoal(apiPort, target.sessionId, target.workspaceId);
      setState((previous) => {
        if (
          !isLatestGoalWrite(requestKey, writeSeq)
          || previous.currentSession?.session_id !== target.sessionId
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
      await reconcileAfterFailure(target, error);
    }
  }, [
    apiPort,
    beginGoalWrite,
    currentSessionId,
    currentWorkspaceId,
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

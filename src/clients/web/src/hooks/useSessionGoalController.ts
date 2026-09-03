import {
  useCallback,
  useEffect,
  useRef,
  type Dispatch,
  type SetStateAction,
} from "react";
import {
  clearSessionGoal as apiClearSessionGoal,
  getSessionGoal as apiGetSessionGoal,
  updateSessionGoal as apiUpdateSessionGoal,
} from "../api";
import type { SessionGoal, SessionGoalUpdateRequest } from "../types/backend";
import type { AppState } from "../types/frontend";

interface GoalTarget {
  sessionId: string;
  workspaceId: string | null;
}

const SESSION_AUXILIARY_LOAD_DELAY_MS = 200;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
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
  setState: Dispatch<SetStateAction<AppState>>;
}) {
  const inFlightGoalRequestsRef = useRef<Map<string, Promise<SessionGoal | null>>>(
    new Map(),
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
    const requestKey = `${apiPort}:${target.workspaceId ?? ""}:${target.sessionId}`;
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
    setState((previous) => ({ ...previous, goalLoading: true, goalError: null }));
    try {
      const goal = await apiUpdateSessionGoal(
        apiPort,
        target.sessionId,
        payload,
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
      return reconcileAfterFailure(target, error);
    }
  }, [apiPort, currentSessionId, currentWorkspaceId, reconcileAfterFailure, setState]);

  const clearGoal = useCallback(async (
    target: GoalTarget = {
      sessionId: currentSessionId ?? "",
      workspaceId: currentWorkspaceId,
    },
  ): Promise<void> => {
    if (!target.sessionId) {
      throw new Error("当前没有可清除 Goal 的会话");
    }
    setState((previous) => ({ ...previous, goalLoading: true, goalError: null }));
    try {
      await apiClearSessionGoal(apiPort, target.sessionId, target.workspaceId);
      setState((previous) => {
        if (previous.currentSession?.session_id !== target.sessionId) {
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
  }, [apiPort, currentSessionId, currentWorkspaceId, reconcileAfterFailure, setState]);

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

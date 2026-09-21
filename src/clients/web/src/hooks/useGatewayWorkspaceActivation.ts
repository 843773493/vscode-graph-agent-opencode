import { useCallback, useRef, type MutableRefObject } from "react";
import { DEFAULT_BACKEND_PORT, listAgents as apiListAgents } from "../api";
import {
  activateGatewayWorkspace as apiActivateGatewayWorkspace,
} from "../gatewayApi";
import type { AppState } from "../types/frontend";
import type { FinishWorkspaceRefresh, SetAppState } from "./contentViewLoaderTypes";
import { createLatestSerialTaskQueue } from "./serialTaskQueue";

type RefreshGatewayWorkspaceStatuses = (
  expectedWorkspaceId?: string | null,
) => Promise<void>;

export function useGatewayWorkspaceActivation({
  apiPort,
  currentSessionId,
  latestStateRef,
  setState,
  invalidateWorkspaceRefreshes,
  refreshGatewayWorkspaceStatuses,
  resetWorkspaceScopedState,
  finishWorkspaceRefresh,
}: {
  apiPort: number | null;
  currentSessionId: string | null;
  latestStateRef: MutableRefObject<AppState>;
  setState: SetAppState;
  invalidateWorkspaceRefreshes: () => void;
  refreshGatewayWorkspaceStatuses: RefreshGatewayWorkspaceStatuses;
  resetWorkspaceScopedState: () => void;
  finishWorkspaceRefresh: FinishWorkspaceRefresh;
}) {
  const workspaceActivationQueueRef = useRef(createLatestSerialTaskQueue());
  const backgroundWorkspaceActivationSequenceRef = useRef(0);

  const activateGatewayWorkspaceInBackground = useCallback((workspaceId: string) => {
    const requestSequence = ++backgroundWorkspaceActivationSequenceRef.current;
    const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
    invalidateWorkspaceRefreshes();
    const operation = workspaceActivationQueueRef.current.enqueue(async () => {
      await apiActivateGatewayWorkspace(resolvedApiPort, workspaceId);
    });
    void operation.then(() => {
      if (requestSequence !== backgroundWorkspaceActivationSequenceRef.current) {
        return;
      }
      void apiListAgents(resolvedApiPort, workspaceId)
        .then((agents) => {
          if (
            requestSequence !== backgroundWorkspaceActivationSequenceRef.current
            || latestStateRef.current.currentSessionWorkspaceId !== workspaceId
          ) {
            return;
          }
          setState((previous) => ({ ...previous, agents }));
        })
        .catch((error: unknown) => {
          if (
            requestSequence !== backgroundWorkspaceActivationSequenceRef.current
            || latestStateRef.current.currentSessionWorkspaceId !== workspaceId
          ) {
            return;
          }
          const message = error instanceof Error ? error.message : String(error);
          setState((previous) => ({
            ...previous,
            gatewayError: "后台加载工作区 Agent 失败: " + message,
            status: "后台加载工作区 Agent 失败: " + message,
          }));
        });
      void refreshGatewayWorkspaceStatuses(workspaceId);
    }).catch((error: unknown) => {
      if (
        requestSequence !== backgroundWorkspaceActivationSequenceRef.current
        || latestStateRef.current.currentSessionWorkspaceId !== workspaceId
      ) {
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      setState((previous) => ({
        ...previous,
        gatewayError: message,
        status: "后台切换工作区失败: " + message,
      }));
    });
  }, [
    apiPort,
    invalidateWorkspaceRefreshes,
    refreshGatewayWorkspaceStatuses,
    setState,
  ]);

  const activateGatewayWorkspace = useCallback(
    (workspaceId: string, preferredSessionId?: string | null) => {
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      backgroundWorkspaceActivationSequenceRef.current += 1;
      invalidateWorkspaceRefreshes();
      resetWorkspaceScopedState();
      const applyActivationFailure = (message: string) => {
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: "工作区切换失败",
          isBootstrapping: false,
        }));
      };
      // latest-only 队列对正式激活有两种吞掉结果的方式：尚未开始的旧任务被直接
      // 跳过，已开始但被顶替的任务其 rejection 也会被队列守卫吞掉。因此结局不能
      // 依赖队列链传播，任务体把状态写入闭包变量，由下面的收尾统一判定，保证
      // 调用方要么拿到真实成功，要么拿到明确失败。
      let started = false;
      let failure: unknown;
      let hasFailure = false;
      const operation = workspaceActivationQueueRef.current.enqueue(async () => {
        started = true;
        try {
          await apiActivateGatewayWorkspace(resolvedApiPort, workspaceId);
          const applied = await finishWorkspaceRefresh(preferredSessionId, {
            checkGatewayWorkspaceHealth: false,
            reuseCurrentUiSettings: true,
          });
          if (applied) {
            void refreshGatewayWorkspaceStatuses(workspaceId);
          }
        } catch (error) {
          failure = error;
          hasFailure = true;
          throw error;
        }
      });
      return operation.catch(() => undefined).then(() => {
        // 无论是否被顶替都要复位 workspaceSwitching：该标志只由正式激活的
        // resetWorkspaceScopedState 置真，后台激活即使成功也不会清理它。
        if (hasFailure) {
          const message = failure instanceof Error ? failure.message : String(failure);
          applyActivationFailure(message);
          throw failure;
        }
        if (!started) {
          const message = `工作区激活已被更新的激活请求取代，${workspaceId} 未生效`;
          applyActivationFailure(message);
          throw new Error(message);
        }
      });
    },
    [
      apiPort,
      finishWorkspaceRefresh,
      invalidateWorkspaceRefreshes,
      refreshGatewayWorkspaceStatuses,
      resetWorkspaceScopedState,
      setState,
    ],
  );

  const refreshGatewayState = useCallback(async () => {
    setState((prev) => ({
      ...prev,
      gatewayError: null,
      error: null,
      isBootstrapping: prev.isBootstrapping || Boolean(prev.error),
      status: "正在刷新 Gateway 状态",
    }));
    try {
      await finishWorkspaceRefresh(currentSessionId);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((prev) => ({
        ...prev,
        gatewayError: message,
        error: message,
        status: `刷新 Gateway 状态失败: ${message}`,
      }));
      throw error;
    }
  }, [currentSessionId, finishWorkspaceRefresh, setState]);

  return {
    activateGatewayWorkspace,
    activateGatewayWorkspaceInBackground,
    refreshGatewayState,
  };
}

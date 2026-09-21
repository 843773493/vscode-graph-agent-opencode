import { useCallback, useRef, type MutableRefObject } from "react";
import { DEFAULT_BACKEND_PORT, listAgents as apiListAgents } from "../api";
import {
  activateGatewayWorkspace as apiActivateGatewayWorkspace,
} from "../gatewayApi";
import type { AppState } from "../types/frontend";
import type { SetAppState } from "./contentViewLoaderTypes";
import { createLatestSerialTaskQueue } from "./serialTaskQueue";

// 激活链路需要把 checkGatewayWorkspaceHealth 与 reuseCurrentUiSettings 透传给
// AppProvider 的 refreshSessions 包装，因此这里必须保留完整签名。
// TODO: useGatewayWorkspaceMutations.ts 与 useGatewayWorkspaceRuntimeLifecycle.ts
// 各自收窄了一份同名类型；待独立提交把准确的 FinishWorkspaceRefresh 收敛到共享
// 模块后再删除此处副本。
type FinishWorkspaceRefresh = (
  preferredSessionId?: string | null,
  options?: {
    checkGatewayWorkspaceHealth?: boolean;
    reuseCurrentUiSettings?: boolean;
  },
) => Promise<boolean>;

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
      return workspaceActivationQueueRef.current.enqueue(async () => {
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
          const message = error instanceof Error ? error.message : String(error);
          setState((prev) => ({
            ...prev,
            workspaceSwitching: false,
            gatewayError: message,
            error: message,
            status: "工作区切换失败",
            isBootstrapping: false,
          }));
          throw error;
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

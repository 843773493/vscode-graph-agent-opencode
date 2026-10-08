import { useCallback } from "react";
import { DEFAULT_BACKEND_PORT } from "../../api";
import {
  forceRestartManagedGatewayWorkspaceBackend as apiForceRestartManagedGatewayWorkspaceBackend,
  probeExternalGatewayWorkspace as apiProbeExternalGatewayWorkspace,
  reconnectGatewayWorkspace as apiReconnectGatewayWorkspace,
  safeRestartManagedGatewayWorkspaceBackend as apiSafeRestartManagedGatewayWorkspaceBackend,
  startManagedGatewayWorkspaceBackend as apiStartManagedGatewayWorkspaceBackend,
  stopManagedGatewayWorkspaceBackend as apiStopManagedGatewayWorkspaceBackend,
} from "../../gatewayApi";
import type {
  GatewayRuntimeRestartResult,
  GatewayRuntimeGroupRequest,
} from "../../types/backend";
import type { FinishWorkspaceRefresh, SetAppState } from "../contentViewLoaderTypes";
import { withFreshGatewayWorkspaceList } from "../../state/gatewayWorkspaceState";
import { errorMessage } from "../../utils/errorMessage";
import { pauseWorkspaceObservations } from "../workspace/workspaceObservationLifecycle";

type RefreshGatewayState = () => Promise<void>;

export function useGatewayWorkspaceRuntimeLifecycle({
  apiPort,
  currentSessionId,
  finishWorkspaceRefresh,
  refreshGatewayState,
  setState,
}: {
  apiPort: number | null;
  currentSessionId: string | null;
  finishWorkspaceRefresh: FinishWorkspaceRefresh;
  refreshGatewayState: RefreshGatewayState;
  setState: SetAppState;
}) {
  const reconnectGatewayWorkspace = useCallback(async (workspaceId: string) => {
    const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
    setState((prev) => ({
      ...prev,
      gatewayError: null,
      status: "正在重新连接工作区",
    }));
    try {
      await apiReconnectGatewayWorkspace(resolvedApiPort, workspaceId);
      await finishWorkspaceRefresh(currentSessionId);
    } catch (error) {
      const message = errorMessage(error);
      setState((prev) => ({
        ...prev,
        gatewayError: message,
        status: `重新连接工作区失败: ${message}`,
      }));
      throw error;
    }
  }, [apiPort, currentSessionId, finishWorkspaceRefresh, setState]);

  const safeRestartManagedGatewayWorkspaceBackend = useCallback(
    async (workspaceId: string, groupRequest?: GatewayRuntimeGroupRequest): Promise<GatewayRuntimeRestartResult> => {
      const resumeObservations = pauseWorkspaceObservations([
        workspaceId,
        ...(groupRequest?.expected_workspace_ids ?? []),
        ...(groupRequest?.desired_workspace_ids ?? []),
      ]);
      try {
        const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
        setState((prev) => ({
          ...prev,
          gatewayError: null,
          status: "正在安全排空并重启 Workspace 后端",
        }));
        try {
          const result = await apiSafeRestartManagedGatewayWorkspaceBackend(
            resolvedApiPort,
            workspaceId,
            groupRequest,
          );
          setState((prev) => ({
            ...withFreshGatewayWorkspaceList(prev, result.workspaces.items),
            activeGatewayWorkspaceId: result.workspaces.active_workspace_id,
            gatewayError: null,
            status: result.status === "blocked"
              ? result.blockers.map((blocker) => `${blocker.workspace_id}: ${blocker.detail ?? blocker.resource_id}`).join("；")
              : result.status === "stopped" ? "Backend 已关闭，成员已退出" : "Backend 完整组已重启",
          }));
          await finishWorkspaceRefresh(currentSessionId);
          return result;
        } catch (error) {
          try {
            await refreshGatewayState();
          } catch {
            // 二次读取失败已由 refreshGatewayState 写入界面。
          }
          const message = errorMessage(error);
          setState((prev) => ({
            ...prev,
            gatewayError: message,
            status: `安全重启 Workspace 后端失败: ${message}`,
          }));
          throw error;
        }
      } finally {
        resumeObservations();
      }
    }, [apiPort, currentSessionId, finishWorkspaceRefresh, refreshGatewayState, setState],
  );

  const startManagedGatewayWorkspaceBackend = useCallback(
    async (workspaceId: string, groupRequest?: GatewayRuntimeGroupRequest): Promise<void> => {
      const resumeObservations = pauseWorkspaceObservations([
        workspaceId,
        ...(groupRequest?.expected_workspace_ids ?? []),
        ...(groupRequest?.desired_workspace_ids ?? []),
      ]);
      try {
        const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
        setState((prev) => ({ ...prev, gatewayError: null, status: "正在启动工作区" }));
        let result;
        try {
          result = await apiStartManagedGatewayWorkspaceBackend(
            resolvedApiPort,
            workspaceId,
            groupRequest,
          );
        } catch (error) {
          try {
            await refreshGatewayState();
          } catch {
            // refreshGatewayState 已将二次读取失败完整写入界面状态。
          }
          const message = errorMessage(error);
          setState((prev) => ({
            ...prev,
            gatewayError: message,
            status: `启动工作区失败: ${message}`,
          }));
          throw error;
        }
        setState((prev) => ({
          ...withFreshGatewayWorkspaceList(prev, result.workspaces.items),
          activeGatewayWorkspaceId: result.workspaces.active_workspace_id,
          gatewayError: null,
          error: null,
          status: result.status === "started" ? "Backend 完整组已启动" : "Backend 完整组未启动",
        }));
        if (result.status === "blocked") {
          const message = result.blockers.map((blocker) => `${blocker.workspace_id}: ${blocker.detail ?? blocker.resource_id}`).join("；");
          setState((prev) => ({ ...prev, gatewayError: message, status: message }));
          throw new Error(message);
        }
        try {
          await finishWorkspaceRefresh(currentSessionId);
        } catch (error) {
          const message = `工作区已启动，但刷新 Gateway 状态失败: ${errorMessage(error)}`;
          setState((prev) => ({
            ...prev,
            workspaceSwitching: false,
            gatewayError: message,
            status: message,
          }));
        }
      } finally {
        resumeObservations();
      }
    }, [apiPort, currentSessionId, finishWorkspaceRefresh, refreshGatewayState, setState],
  );

  const stopManagedGatewayWorkspaceBackend = useCallback(
    async (workspaceId: string, groupRequest?: GatewayRuntimeGroupRequest): Promise<void> => {
      const resumeObservations = pauseWorkspaceObservations([
        workspaceId,
        ...(groupRequest?.expected_workspace_ids ?? []),
        ...(groupRequest?.desired_workspace_ids ?? []),
      ]);
      try {
        const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
        setState((prev) => ({ ...prev, gatewayError: null, status: "正在关闭工作区" }));
        try {
          const result = await apiStopManagedGatewayWorkspaceBackend(
            resolvedApiPort,
            workspaceId,
            groupRequest,
          );
          setState((prev) => ({
            ...withFreshGatewayWorkspaceList(prev, result.workspaces.items),
            activeGatewayWorkspaceId: result.workspaces.active_workspace_id,
            status: result.status === "blocked" ? "工作区仍有活动任务，未关闭" : "工作区已关闭",
          }));
          if (result.status === "blocked") {
            const details = result.blockers
              .map((blocker) => `${blocker.workspace_id}:${blocker.kind}:${blocker.resource_id}`)
              .join("、");
            throw new Error(`工作区仍有 ${result.blockers.length} 个活动任务，未关闭${details ? `（${details}）` : ""}`);
          }
          await finishWorkspaceRefresh(currentSessionId);
        } catch (error) {
          try {
            await refreshGatewayState();
          } catch {
            // refreshGatewayState 已将二次读取失败完整写入界面状态。
          }
          const message = errorMessage(error);
          setState((prev) => ({
            ...prev,
            gatewayError: message,
            status: `关闭工作区失败: ${message}`,
          }));
          throw error;
        }
      } finally {
        resumeObservations();
      }
    }, [apiPort, currentSessionId, finishWorkspaceRefresh, refreshGatewayState, setState],
  );

  const forceRestartManagedGatewayWorkspaceBackend = useCallback(
    async (workspaceId: string, groupRequest?: GatewayRuntimeGroupRequest): Promise<GatewayRuntimeRestartResult> => {
      const resumeObservations = pauseWorkspaceObservations([
        workspaceId,
        ...(groupRequest?.expected_workspace_ids ?? []),
        ...(groupRequest?.desired_workspace_ids ?? []),
      ]);
      try {
        const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
        setState((prev) => ({
          ...prev,
          gatewayError: null,
          status: "正在中断活动任务并强制重启 Workspace 后端",
        }));
        try {
          const result = await apiForceRestartManagedGatewayWorkspaceBackend(
            resolvedApiPort,
            workspaceId,
            groupRequest,
          );
          setState((prev) => ({
            ...withFreshGatewayWorkspaceList(prev, result.workspaces.items),
            activeGatewayWorkspaceId: result.workspaces.active_workspace_id,
            gatewayError: null,
            status: result.status === "blocked"
              ? result.blockers.map((blocker) => `${blocker.workspace_id}: ${blocker.detail ?? blocker.resource_id}`).join("；")
              : result.status === "stopped" ? "Backend 已关闭，成员已退出" : "Backend 完整组已重启",
          }));
          await finishWorkspaceRefresh(currentSessionId);
          return result;
        } catch (error) {
          try {
            await refreshGatewayState();
          } catch {
            // 二次读取失败已由 refreshGatewayState 写入界面。
          }
          const message = errorMessage(error);
          setState((prev) => ({
            ...prev,
            gatewayError: message,
            status: `强制重启 Workspace 后端失败: ${message}`,
          }));
          throw error;
        }
      } finally {
        resumeObservations();
      }
    }, [apiPort, currentSessionId, finishWorkspaceRefresh, refreshGatewayState, setState],
  );

  const probeExternalGatewayWorkspace = useCallback(
    async (workspaceId: string): Promise<void> => {
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      setState((prev) => ({
        ...prev,
        gatewayError: null,
        status: "正在重新探测外部后端",
      }));
      try {
        await apiProbeExternalGatewayWorkspace(resolvedApiPort, workspaceId);
        await finishWorkspaceRefresh(currentSessionId);
      } catch (error) {
        const message = errorMessage(error);
        setState((prev) => ({
          ...prev,
          gatewayError: message,
          status: `重新探测外部后端失败: ${message}`,
        }));
        throw error;
      }
    }, [apiPort, currentSessionId, finishWorkspaceRefresh, refreshGatewayState, setState],
  );

  return {
    reconnectGatewayWorkspace,
    safeRestartManagedGatewayWorkspaceBackend,
    startManagedGatewayWorkspaceBackend,
    stopManagedGatewayWorkspaceBackend,
    forceRestartManagedGatewayWorkspaceBackend,
    probeExternalGatewayWorkspace,
  };
}

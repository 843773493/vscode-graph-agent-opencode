import { useCallback, type MutableRefObject } from "react";
import { DEFAULT_BACKEND_PORT } from "../../api";
import {
  forceRestartManagedGatewayWorkspaceBackend as apiForceRestartManagedGatewayWorkspaceBackend,
  listGatewayWorkspaces as apiListGatewayWorkspaces,
  probeExternalGatewayWorkspace as apiProbeExternalGatewayWorkspace,
  reconnectGatewayWorkspace as apiReconnectGatewayWorkspace,
  safeRestartManagedGatewayWorkspaceBackend as apiSafeRestartManagedGatewayWorkspaceBackend,
  startManagedGatewayWorkspaceBackend as apiStartManagedGatewayWorkspaceBackend,
  stopManagedGatewayWorkspaceBackend as apiStopManagedGatewayWorkspaceBackend,
} from "../../gatewayApi";
import type {
  GatewayRuntimeRestartResult,
  GatewayRuntimeGroupRequest,
  GatewayRuntimeStateResult,
  GatewayWorkspaceList,
} from "../../types/backend";
import type { FinishWorkspaceRefresh, RunGatewayWorkspaceListTask, SetAppState } from "../contentViewLoaderTypes";
import { withFreshGatewayWorkspaceList } from "../../state/gatewayWorkspaceState";
import { errorMessage } from "../../utils/errorMessage";
import { pauseWorkspaceObservations } from "../workspace/workspaceObservationLifecycle";
import { activeGatewayWorkspaceFields } from "./activeGatewayWorkspaceFields";

type RefreshGatewayState = (expectedSelectionIntent?: number) => Promise<void>;

type ReconnectOutcome =
  | { kind: "connected"; workspaces: GatewayWorkspaceList }
  | { kind: "readback"; postError: unknown; workspaces: GatewayWorkspaceList }
  | { kind: "readback-failed"; postError: unknown; readbackError: unknown };

export function useGatewayWorkspaceRuntimeLifecycle({
  apiPort,
  currentSessionId,
  finishWorkspaceRefresh,
  refreshGatewayState,
  setState,
  gatewayListRequestRef,
  runGatewayWorkspaceListTask,
  selectionIntentRef,
}: {
  apiPort: number | null;
  currentSessionId: string | null;
  finishWorkspaceRefresh: FinishWorkspaceRefresh;
  refreshGatewayState: RefreshGatewayState;
  setState: SetAppState;
  gatewayListRequestRef: MutableRefObject<number>;
  runGatewayWorkspaceListTask: RunGatewayWorkspaceListTask;
  selectionIntentRef: MutableRefObject<number>;
}) {
  const reconnectGatewayWorkspace = useCallback(async (workspaceId: string) => {
    const listRequest = ++gatewayListRequestRef.current;
    const selectionIntent = selectionIntentRef.current;
    const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
    const generationIsCurrent = () => listRequest === gatewayListRequestRef.current;
    const selectionIsCurrent = () => selectionIntent === selectionIntentRef.current;
    const requestIsCurrent = () => generationIsCurrent() && selectionIsCurrent();

    setState((prev) => requestIsCurrent() ? ({
      ...prev,
      gatewayError: null,
      status: "正在重新连接工作区",
    }) : prev);

    const outcome = await runGatewayWorkspaceListTask<ReconnectOutcome>(async () => {
      try {
        return {
          kind: "connected",
          workspaces: await apiReconnectGatewayWorkspace(resolvedApiPort, workspaceId),
        };
      } catch (postError) {
        try {
          return {
            kind: "readback",
            postError,
            workspaces: await apiListGatewayWorkspaces(resolvedApiPort),
          };
        } catch (readbackError) {
          return { kind: "readback-failed", postError, readbackError };
        }
      }
    }, (result) => {
      if (result.kind === "readback-failed") {
        setState((prev) => generationIsCurrent()
          ? { ...prev, gatewayWorkspacesStale: true }
          : prev);
        return;
      }

      const { workspaces } = result;
      const activeWorkspaceIsListed = workspaces.active_workspace_id === null
        || workspaces.items.some((item) => item.workspace_id === workspaces.active_workspace_id);
      setState((prev) => {
        if (!generationIsCurrent()) return prev;
        return {
          ...withFreshGatewayWorkspaceList(prev, workspaces.items),
          ...(activeWorkspaceIsListed && selectionIsCurrent() ? activeGatewayWorkspaceFields(workspaces) : {}),
          ...(!activeWorkspaceIsListed ? { gatewayWorkspacesStale: true } : {}),
        };
      });
    });

    if (outcome.kind === "readback-failed") {
      const message = `${errorMessage(outcome.postError)}；读取 Gateway 工作区列表失败: ${errorMessage(outcome.readbackError)}`;
      setState((prev) => {
        if (!requestIsCurrent()) {
          const scopedFailure = `重新连接工作区失败: ${message}`;
          return { ...prev, status: prev.status ? `${prev.status}；${scopedFailure}` : scopedFailure };
        }
        return {
          ...prev,
          gatewayWorkspacesStale: true,
          workspaceSwitching: false,
          gatewayError: message,
          status: `重新连接工作区失败: ${message}`,
        };
      });
      throw outcome.postError;
    }

    const { workspaces } = outcome;
    const activeWorkspaceIsListed = workspaces.active_workspace_id === null
      || workspaces.items.some((item) => item.workspace_id === workspaces.active_workspace_id);
    if (!activeWorkspaceIsListed) {
      const detail = `Gateway 返回的活动工作区不在工作区列表中: ${workspaces.active_workspace_id}`;
      if (outcome.kind === "readback") {
        const message = `${errorMessage(outcome.postError)}；读取 Gateway 工作区列表失败: ${detail}`;
        setState((prev) => {
          if (!requestIsCurrent()) {
            const scopedFailure = `重新连接工作区失败: ${message}`;
            return { ...prev, status: prev.status ? `${prev.status}；${scopedFailure}` : scopedFailure };
          }
          return {
            ...prev,
            gatewayWorkspacesStale: true,
            workspaceSwitching: false,
            gatewayError: message,
            status: `重新连接工作区失败: ${message}`,
          };
        });
        throw outcome.postError;
      }
      const message = `已重新连接，但界面同步失败: ${detail}`;
      setState((prev) => requestIsCurrent() ? ({
        ...prev,
        gatewayWorkspacesStale: true,
        workspaceSwitching: false,
        gatewayError: message,
        status: message,
      }) : prev);
      throw new Error(message);
    }

    if (!requestIsCurrent()) {
      if (outcome.kind === "readback") throw outcome.postError;
      return;
    }

    let expectedListRequest = listRequest;
    let finishError: unknown;
    let finishFailed = false;
    try {
      const refresh = finishWorkspaceRefresh(currentSessionId, { selectionIntent });
      expectedListRequest = gatewayListRequestRef.current;
      const appliedWorkspaceId = await refresh;
      if (appliedWorkspaceId === null) {
        throw new Error("工作区刷新未生效：本轮刷新已被更新的请求作废");
      }
      if (appliedWorkspaceId !== workspaces.active_workspace_id) {
        throw new Error(`工作区刷新结果与 Gateway 活动工作区不一致: ${appliedWorkspaceId}`);
      }
      if (expectedListRequest !== gatewayListRequestRef.current || !selectionIsCurrent()) {
        throw new Error("工作区刷新结果已被更新的请求取代");
      }
    } catch (error) {
      finishError = error;
      finishFailed = true;
    }

    if (finishFailed) {
      const detail = errorMessage(finishError);
      if (outcome.kind === "readback") {
        const message = `${errorMessage(outcome.postError)}；界面同步失败: ${detail}`;
        setState((prev) => {
          if (expectedListRequest !== gatewayListRequestRef.current || !selectionIsCurrent()) {
            const scopedFailure = `重新连接工作区失败: ${message}`;
            return { ...prev, status: prev.status ? `${prev.status}；${scopedFailure}` : scopedFailure };
          }
          return {
            ...prev,
            workspaceSwitching: false,
            gatewayError: message,
            status: `重新连接工作区失败: ${message}`,
          };
        });
        throw outcome.postError;
      }
      const message = `已重新连接，但界面同步失败: ${detail}`;
      setState((prev) => {
        if (expectedListRequest !== gatewayListRequestRef.current || !selectionIsCurrent()) {
          return { ...prev, status: prev.status ? `${prev.status}；${message}` : message };
        }
        return {
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          status: message,
        };
      });
      throw new Error(message);
    }

    if (outcome.kind === "readback") {
      const message = `Gateway 拒绝了重新连接请求，但已读取最新工作区状态: ${errorMessage(outcome.postError)}`;
      setState((prev) => expectedListRequest === gatewayListRequestRef.current && selectionIsCurrent() ? ({
        ...prev,
        workspaceSwitching: false,
        gatewayError: errorMessage(outcome.postError),
        status: message,
      }) : prev);
      throw outcome.postError;
    }
  }, [apiPort, currentSessionId, finishWorkspaceRefresh, gatewayListRequestRef, runGatewayWorkspaceListTask, selectionIntentRef, setState]);

  const safeRestartManagedGatewayWorkspaceBackend = useCallback(
    async (workspaceId: string, groupRequest?: GatewayRuntimeGroupRequest): Promise<GatewayRuntimeRestartResult> => {
      gatewayListRequestRef.current += 1;
      const selectionIntent = selectionIntentRef.current;
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
        let result: GatewayRuntimeRestartResult;
        try {
          result = await runGatewayWorkspaceListTask(() => apiSafeRestartManagedGatewayWorkspaceBackend(
            resolvedApiPort,
            workspaceId,
            groupRequest,
          ), (result) => {
            const actionStatus = result.status === "blocked"
              ? result.blockers.map((blocker) => `${blocker.workspace_id}: ${blocker.detail ?? blocker.resource_id}`).join("；")
              : result.status === "stopped" ? "Backend 已关闭，成员已退出" : "Backend 完整组已重启";
            setState((prev) => ({
              ...withFreshGatewayWorkspaceList(prev, result.workspaces.items),
              ...(selectionIntent === selectionIntentRef.current ? {
                ...activeGatewayWorkspaceFields(result.workspaces), gatewayError: null, status: actionStatus,
              } : {}),
            }));
          });
        } catch (error) {
          try {
            await refreshGatewayState(selectionIntent);
          } catch {
            // 二次读取失败已由 refreshGatewayState 写入界面。
          }
          const message = errorMessage(error);
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({
            ...prev,
            gatewayError: message,
            status: `安全重启 Workspace 后端失败: ${message}`,
          }) : prev);
          throw error;
        }

        const actionStatus = result.status === "blocked"
          ? result.blockers.map((blocker) => `${blocker.workspace_id}: ${blocker.detail ?? blocker.resource_id}`).join("；")
          : result.status === "stopped" ? "Backend 已关闭，成员已退出" : "Backend 完整组已重启";
        try {
          await finishWorkspaceRefresh(currentSessionId, { selectionIntent });
        } catch (error) {
          try {
            await refreshGatewayState(selectionIntent);
          } catch {
            // 二次读取失败已由 refreshGatewayState 写入界面。
          }
          const message = `${actionStatus || "Backend 重启受阻"}；刷新 Gateway 状态失败: ${errorMessage(error)}`;
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({ ...prev, gatewayError: message, status: message }) : prev);
          throw error;
        }
        return result;
      } finally {
        resumeObservations();
      }
    }, [apiPort, currentSessionId, finishWorkspaceRefresh, refreshGatewayState, setState],
  );

  const startManagedGatewayWorkspaceBackend = useCallback(
    async (workspaceId: string, groupRequest?: GatewayRuntimeGroupRequest): Promise<void> => {
      gatewayListRequestRef.current += 1;
      const selectionIntent = selectionIntentRef.current;
      const resumeObservations = pauseWorkspaceObservations([
        workspaceId,
        ...(groupRequest?.expected_workspace_ids ?? []),
        ...(groupRequest?.desired_workspace_ids ?? []),
      ]);
      try {
        const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
        setState((prev) => ({ ...prev, gatewayError: null, status: "正在启动工作区" }));
        let result: GatewayRuntimeStateResult;
        try {
          result = await runGatewayWorkspaceListTask(() => apiStartManagedGatewayWorkspaceBackend(
            resolvedApiPort,
            workspaceId,
            groupRequest,
          ), (result) => {
            setState((prev) => ({
              ...withFreshGatewayWorkspaceList(prev, result.workspaces.items),
              ...(selectionIntent === selectionIntentRef.current ? {
                ...activeGatewayWorkspaceFields(result.workspaces), gatewayError: null, error: null,
                status: result.status === "started" ? "Backend 完整组已启动" : "Backend 完整组未启动",
              } : {}),
            }));
          });
        } catch (error) {
          try {
            await refreshGatewayState(selectionIntent);
          } catch {
            // refreshGatewayState 已将二次读取失败完整写入界面状态。
          }
          const message = errorMessage(error);
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({
            ...prev,
            gatewayError: message,
            status: `启动工作区失败: ${message}`,
          }) : prev);
          throw error;
        }
        if (result.status === "blocked") {
          const message = result.blockers.map((blocker) => `${blocker.workspace_id}: ${blocker.detail ?? blocker.resource_id}`).join("；");
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({ ...prev, gatewayError: message, status: message }) : prev);
          throw new Error(message);
        }
        try {
          await finishWorkspaceRefresh(currentSessionId, { selectionIntent });
        } catch (error) {
          const message = `工作区已启动，但刷新 Gateway 状态失败: ${errorMessage(error)}`;
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({
            ...prev,
            workspaceSwitching: false,
            gatewayError: message,
            status: message,
          }) : prev);
        }
      } finally {
        resumeObservations();
      }
    }, [apiPort, currentSessionId, finishWorkspaceRefresh, refreshGatewayState, setState],
  );

  const stopManagedGatewayWorkspaceBackend = useCallback(
    async (workspaceId: string, groupRequest?: GatewayRuntimeGroupRequest): Promise<void> => {
      gatewayListRequestRef.current += 1;
      const selectionIntent = selectionIntentRef.current;
      const resumeObservations = pauseWorkspaceObservations([
        workspaceId,
        ...(groupRequest?.expected_workspace_ids ?? []),
        ...(groupRequest?.desired_workspace_ids ?? []),
      ]);
      try {
        const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
        setState((prev) => ({ ...prev, gatewayError: null, status: "正在关闭工作区" }));
        let result: GatewayRuntimeStateResult;
        try {
          result = await runGatewayWorkspaceListTask(() => apiStopManagedGatewayWorkspaceBackend(
            resolvedApiPort,
            workspaceId,
            groupRequest,
          ), (result) => {
            setState((prev) => ({
              ...withFreshGatewayWorkspaceList(prev, result.workspaces.items),
              ...(selectionIntent === selectionIntentRef.current ? {
                ...activeGatewayWorkspaceFields(result.workspaces),
                status: result.status === "blocked" ? "工作区仍有活动任务，未关闭" : "工作区已关闭",
              } : {}),
            }));
          });
        } catch (error) {
          try {
            await refreshGatewayState(selectionIntent);
          } catch {
            // refreshGatewayState 已将二次读取失败完整写入界面状态。
          }
          const message = errorMessage(error);
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({
            ...prev,
            gatewayError: message,
            status: `关闭工作区失败: ${message}`,
          }) : prev);
          throw error;
        }

        if (result.status === "blocked") {
          const details = result.blockers
            .map((blocker) => `${blocker.workspace_id}:${blocker.kind}:${blocker.resource_id}`)
            .join("、");
          const error = new Error(`工作区仍有 ${result.blockers.length} 个活动任务，未关闭${details ? `（${details}）` : ""}`);
          try {
            await refreshGatewayState(selectionIntent);
          } catch {
            // 二次读取失败已由 refreshGatewayState 写入界面状态。
          }
          const message = errorMessage(error);
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({
            ...prev,
            gatewayError: message,
            status: `关闭工作区失败: ${message}`,
          }) : prev);
          throw error;
        }
        try {
          await finishWorkspaceRefresh(currentSessionId, { selectionIntent });
        } catch (error) {
          try {
            await refreshGatewayState(selectionIntent);
          } catch {
            // 二次读取失败已由 refreshGatewayState 写入界面状态。
          }
          const message = `工作区已关闭，但刷新 Gateway 状态失败: ${errorMessage(error)}`;
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({ ...prev, gatewayError: message, status: message }) : prev);
          throw error;
        }
      } finally {
        resumeObservations();
      }
    }, [apiPort, currentSessionId, finishWorkspaceRefresh, refreshGatewayState, setState],
  );

  const forceRestartManagedGatewayWorkspaceBackend = useCallback(
    async (workspaceId: string, groupRequest?: GatewayRuntimeGroupRequest): Promise<GatewayRuntimeRestartResult> => {
      gatewayListRequestRef.current += 1;
      const selectionIntent = selectionIntentRef.current;
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
        let result: GatewayRuntimeRestartResult;
        try {
          result = await runGatewayWorkspaceListTask(() => apiForceRestartManagedGatewayWorkspaceBackend(
            resolvedApiPort,
            workspaceId,
            groupRequest,
          ), (result) => {
            const actionStatus = result.status === "blocked"
              ? result.blockers.map((blocker) => `${blocker.workspace_id}: ${blocker.detail ?? blocker.resource_id}`).join("；")
              : result.status === "stopped" ? "Backend 已关闭，成员已退出" : "Backend 完整组已重启";
            setState((prev) => ({
              ...withFreshGatewayWorkspaceList(prev, result.workspaces.items),
              ...(selectionIntent === selectionIntentRef.current ? {
                ...activeGatewayWorkspaceFields(result.workspaces), gatewayError: null, status: actionStatus,
              } : {}),
            }));
          });
        } catch (error) {
          try {
            await refreshGatewayState(selectionIntent);
          } catch {
            // 二次读取失败已由 refreshGatewayState 写入界面。
          }
          const message = errorMessage(error);
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({
            ...prev,
            gatewayError: message,
            status: `强制重启 Workspace 后端失败: ${message}`,
          }) : prev);
          throw error;
        }

        const actionStatus = result.status === "blocked"
          ? result.blockers.map((blocker) => `${blocker.workspace_id}: ${blocker.detail ?? blocker.resource_id}`).join("；")
          : result.status === "stopped" ? "Backend 已关闭，成员已退出" : "Backend 完整组已重启";
        try {
          await finishWorkspaceRefresh(currentSessionId, { selectionIntent });
        } catch (error) {
          try {
            await refreshGatewayState(selectionIntent);
          } catch {
            // 二次读取失败已由 refreshGatewayState 写入界面。
          }
          const message = result.status === "blocked"
            ? `${actionStatus || "Backend 重启受阻"}；刷新 Gateway 状态失败: ${errorMessage(error)}`
            : `${actionStatus}，但刷新 Gateway 状态失败: ${errorMessage(error)}`;
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({ ...prev, gatewayError: message, status: message }) : prev);
          throw error;
        }
        return result;
      } finally {
        resumeObservations();
      }
    }, [apiPort, currentSessionId, finishWorkspaceRefresh, refreshGatewayState, setState],
  );

  const probeExternalGatewayWorkspace = useCallback(
    async (workspaceId: string): Promise<void> => {
      gatewayListRequestRef.current += 1;
      const selectionIntent = selectionIntentRef.current;
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      setState((prev) => ({
        ...prev,
        gatewayError: null,
        status: "正在重新探测外部后端",
      }));
      try {
        await runGatewayWorkspaceListTask(() => apiProbeExternalGatewayWorkspace(resolvedApiPort, workspaceId), (workspaces) => {
          setState((prev) => ({
            ...withFreshGatewayWorkspaceList(prev, workspaces.items),
            ...(selectionIntent === selectionIntentRef.current ? activeGatewayWorkspaceFields(workspaces) : {}),
          }));
        });
        await finishWorkspaceRefresh(currentSessionId, { selectionIntent });
      } catch (error) {
        const message = errorMessage(error);
        setState((prev) => selectionIntent === selectionIntentRef.current ? ({
          ...prev,
          gatewayError: message,
          status: `重新探测外部后端失败: ${message}`,
        }) : prev);
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

import { useCallback } from "react";
import { DEFAULT_BACKEND_PORT } from "../../api";
import {
  addManagedGatewayWorkspace as apiAddManagedGatewayWorkspace,
  addSshGatewayWorkspace as apiAddSshGatewayWorkspace,
  listGatewayWorkspaces as apiListGatewayWorkspaces,
  removeGatewayWorkspace as apiRemoveGatewayWorkspace,
  renameGatewayWorkspace as apiRenameGatewayWorkspace,
  reorderGatewayWorkspaces as apiReorderGatewayWorkspaces,
} from "../../gatewayApi";
import type {
  AddManagedGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "../../types/backend";
import {
  applyGatewayWorkspaceListAfterRemoval,
  withFreshGatewayWorkspaceList,
} from "../../state/gatewayWorkspaceState";
import type { FinishWorkspaceRefresh, SetAppState } from "../contentViewLoaderTypes";

export function useGatewayWorkspaceMutations({
  apiPort,
  activeGatewayWorkspaceId,
  recentLocalWorkspacePaths,
  setState,
  abortCurrentStream,
  invalidateWorkspaceRefreshes,
  finishWorkspaceRefresh,
  resetWorkspaceScopedState,
  updateUiSettings,
}: {
  apiPort: number | null;
  activeGatewayWorkspaceId: string | null;
  recentLocalWorkspacePaths: WebUiSettings["recent_local_workspace_paths"];
  setState: SetAppState;
  abortCurrentStream: () => void;
  invalidateWorkspaceRefreshes: () => void;
  finishWorkspaceRefresh: FinishWorkspaceRefresh;
  resetWorkspaceScopedState: () => void;
  updateUiSettings: (
    input: WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate),
  ) => Promise<void>;
}) {
  const addManagedGatewayWorkspace = useCallback(
    async (payload: AddManagedGatewayWorkspaceRequest) => {
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      try {
        await apiAddManagedGatewayWorkspace(resolvedApiPort, payload);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: `添加工作区失败: ${message}`,
          isBootstrapping: false,
        }));
        throw error;
      }

      const reconciliationErrors: string[] = [];
      const normalizedPath = payload.root_path.trim();
      if (normalizedPath && !payload.gateway_connection_id) {
        try {
          const recentPaths = [
            normalizedPath,
            ...recentLocalWorkspacePaths,
          ].filter(
            (path, index, paths) =>
              path.trim() && paths.findIndex((item) => item === path) === index,
          );
          await updateUiSettings({
            recent_local_workspace_paths: recentPaths,
          });
        } catch (error) {
          reconciliationErrors.push(
            `保存最近路径失败: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
      }
      try {
        await finishWorkspaceRefresh();
      } catch (error) {
        reconciliationErrors.push(
          `刷新工作区列表失败: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
      if (reconciliationErrors.length > 0) {
        const message = `工作区已添加，但界面同步失败: ${reconciliationErrors.join("；")}`;
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: message,
          isBootstrapping: false,
        }));
        throw new Error(message);
      }
    },
    [apiPort, finishWorkspaceRefresh, recentLocalWorkspacePaths, setState, updateUiSettings],
  );

  const addSshGatewayWorkspace = useCallback(
    async (payload: AddSshGatewayWorkspaceRequest) => {
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      resetWorkspaceScopedState();
      try {
        await apiAddSshGatewayWorkspace(resolvedApiPort, payload);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: `连接远程 Gateway 失败: ${message}`,
          isBootstrapping: false,
        }));
        throw error;
      }
      try {
        await finishWorkspaceRefresh();
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        const message = `远程 Gateway 已连接，但界面同步失败: ${detail}`;
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: message,
          isBootstrapping: false,
        }));
        throw new Error(message);
      }
    },
    [apiPort, finishWorkspaceRefresh, resetWorkspaceScopedState, setState],
  );

  const removeGatewayWorkspace = useCallback(
    async (workspaceId: string) => {
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      const removedActiveWorkspace = workspaceId === activeGatewayWorkspaceId;
      let workspaceRemoved = false;
      invalidateWorkspaceRefreshes();
      setState((prev) => ({
        ...prev,
        removingGatewayWorkspaceIds: new Set([
          ...prev.removingGatewayWorkspaceIds,
          workspaceId,
        ]),
        gatewayError: null,
        error: null,
        status: "正在删除工作区",
      }));
      try {
        const workspaceList = await apiRemoveGatewayWorkspace(
          resolvedApiPort,
          workspaceId,
        );
        workspaceRemoved = true;
        const activeWorkspaceChanged =
          workspaceList.active_workspace_id !== activeGatewayWorkspaceId;
        if (removedActiveWorkspace || activeWorkspaceChanged) {
          abortCurrentStream();
        }
        setState((prev) => {
          const reconciledState = applyGatewayWorkspaceListAfterRemoval(
            prev,
            workspaceId,
            workspaceList,
          );
          if (!removedActiveWorkspace && !activeWorkspaceChanged) {
            return reconciledState;
          }
          const activeWorkspace = workspaceList.items.find(
            (workspace) =>
              workspace.workspace_id === workspaceList.active_workspace_id,
          );
          return {
            ...reconciledState,
            workspaceSwitching: true,
            workspaceRoot: activeWorkspace?.root_path ?? null,
            workspaceName: activeWorkspace?.name ?? null,
            sessions: workspaceList.active_workspace_id
              ? reconciledState.sessionsByWorkspace.get(
                  workspaceList.active_workspace_id,
                ) ?? []
              : [],
            currentSession: null,
            currentSessionWorkspaceId: null,
            traceEvents: [],
            llmRequestLogs: [],
            sessionResources: [],
            agentStateJsonl: "",
            agentStateMessageCount: 0,
          };
        });
        await updateUiSettings((current) => {
          const expandedPathsByWorkspace = {
            ...current.workspace_file_tree.expanded_paths_by_workspace,
          };
          delete expandedPathsByWorkspace[workspaceId];
          return {
            session_sidebar: {
              collapsed_workspace_ids:
                current.session_sidebar.collapsed_workspace_ids.filter(
                  (collapsedId) => collapsedId !== workspaceId,
                ),
              expanded_root_tree_ids:
                current.session_sidebar.expanded_root_tree_ids.filter(
                  (treeId) => treeId !== `workspace:${workspaceId}`,
                ),
            },
            workspace_file_tree: {
              expanded_paths_by_workspace: expandedPathsByWorkspace,
            },
          };
        });
        if (removedActiveWorkspace || activeWorkspaceChanged) {
          await finishWorkspaceRefresh();
        }
      } catch (error) {
        const errorMessage =
          error instanceof Error ? error.message : String(error);
        const operationMessage = workspaceRemoved
          ? `工作区已删除，但新活动工作区加载失败: ${errorMessage}`
          : errorMessage;
        let reconciliationMessage: string | null = null;
        try {
          const workspaceList = await apiListGatewayWorkspaces(resolvedApiPort);
          setState((prev) => {
            const removingGatewayWorkspaceIds = new Set(
              prev.removingGatewayWorkspaceIds,
            );
            removingGatewayWorkspaceIds.delete(workspaceId);
            return {
              ...withFreshGatewayWorkspaceList(prev, workspaceList.items),
              activeGatewayWorkspaceId: workspaceList.active_workspace_id,
              removingGatewayWorkspaceIds,
            };
          });
        } catch (reconciliationError) {
          reconciliationMessage =
            reconciliationError instanceof Error
              ? reconciliationError.message
              : String(reconciliationError);
        }
        const message = reconciliationMessage
          ? `${operationMessage}；重新读取工作区列表也失败: ${reconciliationMessage}`
          : operationMessage;
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          removingGatewayWorkspaceIds: new Set(
            [...prev.removingGatewayWorkspaceIds].filter(
              (removingId) => removingId !== workspaceId,
            ),
          ),
          gatewayError: message,
          error: message,
          status: workspaceRemoved
            ? message
            : `删除工作区失败: ${message}`,
          isBootstrapping: false,
        }));
        throw error;
      }
    },
    [
      abortCurrentStream,
      activeGatewayWorkspaceId,
      apiPort,
      finishWorkspaceRefresh,
      invalidateWorkspaceRefreshes,
      setState,
      updateUiSettings,
    ],
  );

  const reorderGatewayWorkspaces = useCallback(
    async (workspaceIds: string[]) => {
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      try {
        const workspaceList = await apiReorderGatewayWorkspaces(resolvedApiPort, {
          workspace_ids: workspaceIds,
        });
        setState((prev) => {
          const activeWorkspaceId =
            workspaceList.active_workspace_id ?? prev.activeGatewayWorkspaceId;
          const activeWorkspace = workspaceList.items.find(
            (workspace) => workspace.workspace_id === activeWorkspaceId,
          );
          return {
            ...withFreshGatewayWorkspaceList(prev, workspaceList.items),
            activeGatewayWorkspaceId: activeWorkspaceId,
            workspaceRoot: activeWorkspace?.root_path ?? prev.workspaceRoot,
            workspaceName: activeWorkspace?.name ?? prev.workspaceName,
            status: "工作区顺序已更新",
          };
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          gatewayError: message,
          error: message,
          status: `工作区排序失败: ${message}`,
        }));
        throw error;
      }
    },
    [apiPort, setState],
  );

  const renameGatewayWorkspace = useCallback(
    async (workspaceId: string, name: string) => {
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      try {
        const workspaceList = await apiRenameGatewayWorkspace(
          resolvedApiPort,
          workspaceId,
          { name },
        );
        const renamedWorkspace = workspaceList.items.find(
          (workspace) => workspace.workspace_id === workspaceId,
        );
        if (!renamedWorkspace) {
          throw new Error(`Gateway 重命名响应缺少工作区: ${workspaceId}`);
        }
        setState((prev) => {
          const activeWorkspace = workspaceList.items.find(
            (workspace) =>
              workspace.workspace_id === workspaceList.active_workspace_id,
          );
          return {
            ...withFreshGatewayWorkspaceList(prev, workspaceList.items),
            activeGatewayWorkspaceId: workspaceList.active_workspace_id,
            workspaceRoot: activeWorkspace?.root_path ?? null,
            workspaceName: activeWorkspace?.name ?? null,
            gatewayError: null,
            error: null,
            status: `工作区已重命名为「${renamedWorkspace.name}」`,
          };
        });
        return renamedWorkspace.name;
      } catch (error) {
        const operationMessage =
          error instanceof Error ? error.message : String(error);
        let message = operationMessage;
        try {
          const workspaceList = await apiListGatewayWorkspaces(resolvedApiPort);
          setState((prev) => {
            const activeWorkspace = workspaceList.items.find(
              (workspace) =>
                workspace.workspace_id === workspaceList.active_workspace_id,
            );
            return {
              ...withFreshGatewayWorkspaceList(prev, workspaceList.items),
              activeGatewayWorkspaceId: workspaceList.active_workspace_id,
              workspaceRoot: activeWorkspace?.root_path ?? null,
              workspaceName: activeWorkspace?.name ?? null,
            };
          });
        } catch (reconciliationError) {
          const reconciliationMessage = reconciliationError instanceof Error
            ? reconciliationError.message
            : String(reconciliationError);
          message = `${operationMessage}；重新读取工作区列表也失败: ${reconciliationMessage}`;
        }
        setState((prev) => ({
          ...prev,
          gatewayError: message,
          error: message,
          status: `重命名工作区失败: ${message}`,
        }));
        throw new Error(message);
      }
    },
    [apiPort, setState],
  );

  return {
    addManagedGatewayWorkspace,
    addSshGatewayWorkspace,
    removeGatewayWorkspace,
    reorderGatewayWorkspaces,
    renameGatewayWorkspace,
  };
}

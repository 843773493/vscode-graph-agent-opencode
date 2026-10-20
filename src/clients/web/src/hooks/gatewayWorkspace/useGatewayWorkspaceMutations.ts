import { useCallback, useRef, type MutableRefObject } from "react";
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
  GatewayRuntimeGroupRequest,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "../../types/backend";
import {
  applyGatewayWorkspaceListAfterRemoval,
  withFreshGatewayWorkspaceList,
} from "../../state/gatewayWorkspaceState";
import type { FinishWorkspaceRefresh, RunGatewayWorkspaceListTask, SetAppState } from "../contentViewLoaderTypes";
import { errorMessage } from "../../utils/errorMessage";
import { activeGatewayWorkspaceFields } from "./activeGatewayWorkspaceFields";
import { pauseWorkspaceObservations } from "../workspace/workspaceObservationLifecycle";

export function useGatewayWorkspaceMutations({
  apiPort,
  activeGatewayWorkspaceId,
  setState,
  abortCurrentStream,
  invalidateWorkspaceRefreshes,
  finishWorkspaceRefresh,
  resetWorkspaceScopedState,
  updateUiSettings,
  gatewayListRequestRef,
  runGatewayWorkspaceListTask,
  selectionIntentRef,
}: {
  apiPort: number | null;
  activeGatewayWorkspaceId: string | null;
  setState: SetAppState;
  abortCurrentStream: () => void;
  invalidateWorkspaceRefreshes: () => void;
  finishWorkspaceRefresh: FinishWorkspaceRefresh;
  resetWorkspaceScopedState: () => void;
  gatewayListRequestRef: MutableRefObject<number>;
  runGatewayWorkspaceListTask: RunGatewayWorkspaceListTask;
  selectionIntentRef: MutableRefObject<number>;
  updateUiSettings: (
    input: WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate),
  ) => Promise<void>;
}) {
  const removalOwnerByWorkspaceRef = useRef(new Map<string, number>());
  const addManagedGatewayWorkspace = useCallback(
    async (payload: AddManagedGatewayWorkspaceRequest) => {
      const selectionIntent = selectionIntentRef.current;
      const listRequest = ++gatewayListRequestRef.current;
      const activeWorkspaceIdAtStart = activeGatewayWorkspaceId;
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      let postFailed = false;
      let postError: unknown;
      let listReadFailed = false;
      let listReadError: unknown;
      let activeWorkspaceChanged = false;
      await runGatewayWorkspaceListTask(async () => {
        try {
          await apiAddManagedGatewayWorkspace(resolvedApiPort, payload);
        } catch (error) {
          postFailed = true;
          postError = error;
        }
        try {
          const workspaces = await apiListGatewayWorkspaces(resolvedApiPort);
          activeWorkspaceChanged = workspaces.active_workspace_id !== activeWorkspaceIdAtStart;
          setState((prev) => listRequest === gatewayListRequestRef.current ? ({
            ...withFreshGatewayWorkspaceList(prev, workspaces.items),
            ...(selectionIntent === selectionIntentRef.current
              ? activeGatewayWorkspaceFields(workspaces)
              : {}),
          }) : prev);
        } catch (error) {
          listReadFailed = true;
          listReadError = error;
          setState((prev) => listRequest === gatewayListRequestRef.current
            ? { ...prev, gatewayWorkspacesStale: true }
            : prev);
        }
      });

      let expectedListRequest = listRequest;
      let refreshError: string | null = null;
      let refreshApplied = false;
      if (postFailed && !listReadFailed && activeWorkspaceChanged) {
        if (listRequest === gatewayListRequestRef.current
          && selectionIntent === selectionIntentRef.current) {
          try {
            const refresh = finishWorkspaceRefresh(undefined, { selectionIntent });
            expectedListRequest = gatewayListRequestRef.current;
            const appliedWorkspaceId = await refresh;
            if (appliedWorkspaceId === null) {
              refreshError = "刷新工作区列表未生效：本轮刷新已被更新的请求作废";
            } else if (expectedListRequest === listRequest) {
              refreshError = "刷新工作区列表未生效：未取得新的列表代次";
            } else if (expectedListRequest !== gatewayListRequestRef.current
              || selectionIntent !== selectionIntentRef.current) {
              refreshError = "刷新工作区列表结果已被更新的请求取代";
            } else {
              refreshApplied = true;
            }
          } catch (error) {
            refreshError = `刷新工作区列表失败: ${errorMessage(error)}`;
          }
        } else {
          refreshError = "活动工作区已变化，但刷新工作区列表未执行：当前列表请求或工作区选择已更新";
        }
      }

      if (postFailed) {
        const details = [
          errorMessage(postError),
          ...(listReadFailed
            ? [`读取 Gateway 工作区列表失败: ${errorMessage(listReadError)}`]
            : []),
          ...(refreshError ? [refreshError] : []),
        ];
        const message = details.join("；");
        const scopedFailure = `工作区 root_path="${payload.root_path}" 添加失败: ${message}`;
        setState((prev) => {
          if (expectedListRequest !== gatewayListRequestRef.current
            || selectionIntent !== selectionIntentRef.current) {
            return {
              ...prev,
              status: prev.status ? `${prev.status}；${scopedFailure}` : scopedFailure,
            };
          }
          return {
            ...prev,
            workspaceSwitching: false,
            gatewayError: message,
            status: `添加工作区失败: ${message}`,
            isBootstrapping: false,
          };
        });
        throw postError;
      }

      const reconciliationErrors: string[] = [];
      if (listReadFailed) {
        reconciliationErrors.push(`读取 Gateway 工作区列表失败: ${errorMessage(listReadError)}`);
      }
      if (listRequest === gatewayListRequestRef.current
        && selectionIntent === selectionIntentRef.current) {
        try {
          const refresh = finishWorkspaceRefresh(undefined, { selectionIntent });
          expectedListRequest = gatewayListRequestRef.current;
          const appliedWorkspaceId = await refresh;
          if (appliedWorkspaceId === null) {
            reconciliationErrors.push(
              "刷新工作区列表未生效：本轮刷新已被更新的请求作废",
            );
          } else if (expectedListRequest === listRequest) {
            reconciliationErrors.push("刷新工作区列表未生效：未取得新的列表代次");
          } else if (expectedListRequest !== gatewayListRequestRef.current
            || selectionIntent !== selectionIntentRef.current) {
            reconciliationErrors.push("刷新工作区列表结果已被更新的请求取代");
          } else {
            refreshApplied = true;
          }
        } catch (error) {
          reconciliationErrors.push(`刷新工作区列表失败: ${errorMessage(error)}`);
        }
      } else if (listReadFailed) {
        reconciliationErrors.push(
          "刷新工作区列表未执行：当前列表请求或工作区选择已更新",
        );
      }

      if (listReadFailed && refreshApplied) {
        reconciliationErrors.splice(
          reconciliationErrors.findIndex((error) =>
            error.startsWith("读取 Gateway 工作区列表失败:")),
          1,
        );
      }
      if (reconciliationErrors.length > 0) {
        const message = `工作区已添加，但界面同步失败: ${reconciliationErrors.join("；")}`;
        const scopedFailure = `工作区 root_path="${payload.root_path}" 已添加，但界面同步失败: ${reconciliationErrors.join("；")}`;
        setState((prev) => {
          if (expectedListRequest !== gatewayListRequestRef.current
            || selectionIntent !== selectionIntentRef.current) {
            return {
              ...prev,
              status: prev.status ? `${prev.status}；${scopedFailure}` : scopedFailure,
            };
          }
          return {
            ...prev,
            workspaceSwitching: false,
            gatewayError: message,
            status: message,
            isBootstrapping: false,
          };
        });
        throw new Error(message);
      }
    },
    [activeGatewayWorkspaceId, apiPort, finishWorkspaceRefresh, gatewayListRequestRef,
      runGatewayWorkspaceListTask, selectionIntentRef, setState],
  );

  const addSshGatewayWorkspace = useCallback(
    async (payload: AddSshGatewayWorkspaceRequest) => {
      const selectionIntent = selectionIntentRef.current;
      const listRequest = ++gatewayListRequestRef.current;
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      resetWorkspaceScopedState();
      const requestIsCurrent = () =>
        listRequest === gatewayListRequestRef.current
        && selectionIntent === selectionIntentRef.current;
      await runGatewayWorkspaceListTask(async () => {
        try {
          return await apiAddSshGatewayWorkspace(resolvedApiPort, payload);
        } catch (error) {
          let message = errorMessage(error);
          if (requestIsCurrent()) {
            try {
              const workspaces = await apiListGatewayWorkspaces(resolvedApiPort);
              setState((prev) => requestIsCurrent() ? ({
                ...withFreshGatewayWorkspaceList(prev, workspaces.items),
                ...activeGatewayWorkspaceFields(workspaces),
              }) : prev);
            } catch (reconciliationError) {
              message += `；读取 Gateway 工作区列表失败: ${errorMessage(reconciliationError)}`;
              setState((prev) => requestIsCurrent()
                ? { ...prev, gatewayWorkspacesStale: true }
                : prev);
            }
          }
          setState((prev) => requestIsCurrent() ? ({
            ...prev,
            workspaceSwitching: false,
            gatewayError: message,
            status: `连接远程 Gateway 失败: ${message}`,
            isBootstrapping: false,
          }) : prev);
          throw error;
        }
      }, (workspaces) => {
        setState((prev) => ({
          ...withFreshGatewayWorkspaceList(prev, workspaces.items),
          ...(selectionIntent === selectionIntentRef.current ? activeGatewayWorkspaceFields(workspaces) : {}),
        }));
      });
      try {
        await finishWorkspaceRefresh(undefined, { selectionIntent });
      } catch (error) {
        const detail = errorMessage(error);
        const message = `远程 Gateway 已连接，但界面同步失败: ${detail}`;
        setState((prev) => selectionIntent === selectionIntentRef.current ? ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          status: message,
          isBootstrapping: false,
        }) : prev);
        throw new Error(message);
      }
    },
    [apiPort, finishWorkspaceRefresh, resetWorkspaceScopedState, setState],
  );

  const removeGatewayWorkspace = useCallback(
    async (workspaceId: string, groupRequest?: GatewayRuntimeGroupRequest, force = false) => {
      const resumeObservations = pauseWorkspaceObservations([
        workspaceId, ...(groupRequest?.expected_workspace_ids ?? []),
        ...(groupRequest?.desired_workspace_ids ?? []),
      ]);
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      const removedActiveWorkspace = workspaceId === activeGatewayWorkspaceId;
      let workspaceRemoved = false;
      let postCommitFailureStage: "删除响应处理" | "本地界面设置清理" | "新活动工作区加载" | null = null;
      let activeWorkspaceChanged = false;
      invalidateWorkspaceRefreshes();
      const listRequest = ++gatewayListRequestRef.current;
      removalOwnerByWorkspaceRef.current.set(workspaceId, listRequest);
      let reconciliationGeneration = listRequest;
      const selectionIntent = selectionIntentRef.current;
      const listRequestIsCurrent = () => listRequest === gatewayListRequestRef.current;
      const operationIsCurrent = () =>
        listRequestIsCurrent() && selectionIntent === selectionIntentRef.current;
      setState((prev) => operationIsCurrent() ? ({
        ...prev,
        removingGatewayWorkspaceIds: new Set([
          ...prev.removingGatewayWorkspaceIds,
          workspaceId,
        ]),
        gatewayError: null,
        error: null,
        status: "正在删除工作区",
      }) : prev);
      try {
        await runGatewayWorkspaceListTask(() => apiRemoveGatewayWorkspace(
          resolvedApiPort,
          workspaceId,
          groupRequest,
          force,
        ), (workspaceList) => {
        workspaceRemoved = true;
        postCommitFailureStage = "删除响应处理";
        if (!listRequestIsCurrent()) return;
        activeWorkspaceChanged =
          workspaceList.active_workspace_id !== activeGatewayWorkspaceId;
        if (selectionIntent === selectionIntentRef.current && (removedActiveWorkspace || activeWorkspaceChanged)) {
          abortCurrentStream();
        }
        setState((prev) => {
          if (!listRequestIsCurrent()) return prev;
          const reconciledState = applyGatewayWorkspaceListAfterRemoval(
            prev,
            workspaceId,
            workspaceList,
            selectionIntent === selectionIntentRef.current,
          );
          if (selectionIntent !== selectionIntentRef.current || (!removedActiveWorkspace && !activeWorkspaceChanged)) {
            return reconciledState;
          }
          return {
            ...reconciledState,
            ...activeGatewayWorkspaceFields(workspaceList),
            workspaceSwitching: true,
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
        });
        postCommitFailureStage = "本地界面设置清理";
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
        postCommitFailureStage = null;
        if (listRequestIsCurrent() && selectionIntent === selectionIntentRef.current
          && (removedActiveWorkspace || activeWorkspaceChanged)) {
          postCommitFailureStage = "新活动工作区加载";
          const refresh = finishWorkspaceRefresh(undefined, { selectionIntent });
          // finishWorkspaceRefresh 自己推进刷新代次；后续删除对账只能沿用它取得的代次。
          reconciliationGeneration = gatewayListRequestRef.current;
          await refresh;
          postCommitFailureStage = null;
        }
      } catch (error) {
        const workspaceFailureMessage = errorMessage(error);
        const postCommitStage = postCommitFailureStage ?? "删除响应处理";
        const operationMessage = workspaceRemoved
          ? `工作区已删除，但${postCommitStage}失败: ${workspaceFailureMessage}`
          : workspaceFailureMessage;
        let reconciliationMessage: string | null = null;
        const reconciliationIsCurrent = () =>
          reconciliationGeneration === gatewayListRequestRef.current;
        if (reconciliationIsCurrent()) {
          try {
            const workspaceList = await runGatewayWorkspaceListTask(() => apiListGatewayWorkspaces(resolvedApiPort));
            setState((prev) => {
              if (!reconciliationIsCurrent()) return prev;
              const removingGatewayWorkspaceIds = new Set(
                prev.removingGatewayWorkspaceIds,
              );
              removingGatewayWorkspaceIds.delete(workspaceId);
              return {
                ...withFreshGatewayWorkspaceList(prev, workspaceList.items),
                ...(selectionIntent === selectionIntentRef.current ? activeGatewayWorkspaceFields(workspaceList) : {}),
                removingGatewayWorkspaceIds,
              };
            });
          } catch (reconciliationError) {
            reconciliationMessage = errorMessage(reconciliationError);
            setState((prev) => reconciliationIsCurrent()
              ? { ...prev, gatewayWorkspacesStale: true }
              : prev);
          }
        }
        const message = reconciliationMessage
          ? `${operationMessage}；重新读取工作区列表也失败: ${reconciliationMessage}`
          : operationMessage;
        const scopedFailure = workspaceRemoved
          ? `工作区 ${workspaceId} 删除已提交，但${postCommitStage}失败: ${workspaceFailureMessage}${reconciliationMessage
            ? `；重新读取工作区列表也失败: ${reconciliationMessage}`
            : ""}`
          : `工作区 ${workspaceId} 删除失败: ${message}`;
        setState((prev) => {
          if (reconciliationIsCurrent() && selectionIntent === selectionIntentRef.current) {
            return {
              ...prev,
              workspaceSwitching: false,
              gatewayError: message,
              status: workspaceRemoved ? message : `删除工作区失败: ${message}`,
              isBootstrapping: false,
            };
          }
          return {
            ...prev,
            status: prev.status ? `${prev.status}；${scopedFailure}` : scopedFailure,
          };
        });
        throw error;
      } finally {
        const ownsRemovalMarker = removalOwnerByWorkspaceRef.current.get(workspaceId) === listRequest;
        if (ownsRemovalMarker) removalOwnerByWorkspaceRef.current.delete(workspaceId);
        if (ownsRemovalMarker) {
          setState((prev) => {
            if (removalOwnerByWorkspaceRef.current.has(workspaceId)) return prev;
            const removingGatewayWorkspaceIds = new Set(prev.removingGatewayWorkspaceIds);
            removingGatewayWorkspaceIds.delete(workspaceId);
            return { ...prev, removingGatewayWorkspaceIds };
          });
        }
        resumeObservations();
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
      const listRequest = ++gatewayListRequestRef.current;
      const selectionIntent = selectionIntentRef.current;
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      return runGatewayWorkspaceListTask(async () => {
        try {
          const workspaceList = await apiReorderGatewayWorkspaces(resolvedApiPort, {
            workspace_ids: workspaceIds,
          });
          setState((prev) => listRequest === gatewayListRequestRef.current ? {
            ...withFreshGatewayWorkspaceList(prev, workspaceList.items),
            ...(selectionIntent === selectionIntentRef.current ? {
              ...activeGatewayWorkspaceFields(workspaceList), status: "工作区顺序已更新",
            } : {}),
          } : prev);
        } catch (error) {
          const operationMessage = errorMessage(error);
          let message = operationMessage;
          let workspaceList: Awaited<ReturnType<typeof apiListGatewayWorkspaces>> | null = null;
          try {
            workspaceList = await apiListGatewayWorkspaces(resolvedApiPort);
          } catch (reconciliationError) {
            message += `；重新读取工作区列表也失败: ${errorMessage(reconciliationError)}`;
          }
          setState((prev) => {
            if (listRequest !== gatewayListRequestRef.current) return prev;
            const reconciled = workspaceList
              ? withFreshGatewayWorkspaceList(prev, workspaceList.items)
              : { ...prev, gatewayWorkspacesStale: true };
            return selectionIntent === selectionIntentRef.current ? {
              ...reconciled,
              ...(workspaceList ? activeGatewayWorkspaceFields(workspaceList) : {}),
              gatewayError: message,
              status: `工作区排序失败: ${message}`,
            } : reconciled;
          });
          throw error;
        }
      });
    },
    [apiPort, gatewayListRequestRef, runGatewayWorkspaceListTask, selectionIntentRef, setState],
  );

  const renameGatewayWorkspace = useCallback(
    async (workspaceId: string, name: string) => {
      const listRequest = ++gatewayListRequestRef.current;
      const selectionIntent = selectionIntentRef.current;
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      return runGatewayWorkspaceListTask(async () => {
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
            if (listRequest !== gatewayListRequestRef.current) return prev;
            return {
              ...withFreshGatewayWorkspaceList(prev, workspaceList.items),
              ...(selectionIntent === selectionIntentRef.current ? {
                ...activeGatewayWorkspaceFields(workspaceList), gatewayError: null, error: null,
                status: `工作区已重命名为「${renamedWorkspace.name}」`,
              } : {}),
            };
          });
          return renamedWorkspace.name;
        } catch (error) {
          const operationMessage = errorMessage(error);
          let message = operationMessage;
          let workspaceList: Awaited<ReturnType<typeof apiListGatewayWorkspaces>> | null = null;
          try {
            workspaceList = await apiListGatewayWorkspaces(resolvedApiPort);
          } catch (reconciliationError) {
            const reconciliationMessage = errorMessage(reconciliationError);
            message = `${operationMessage}；重新读取工作区列表也失败: ${reconciliationMessage}`;
          }
          setState((prev) => {
            if (listRequest !== gatewayListRequestRef.current) return prev;
            const reconciled = workspaceList
              ? withFreshGatewayWorkspaceList(prev, workspaceList.items)
              : { ...prev, gatewayWorkspacesStale: true };
            return selectionIntent === selectionIntentRef.current
              ? {
                ...reconciled,
                ...(workspaceList ? activeGatewayWorkspaceFields(workspaceList) : {}),
                gatewayError: message,
                status: `重命名工作区失败: ${message}`,
              }
              : reconciled;
          });
          throw error;
        }
      });
    },
    [apiPort, gatewayListRequestRef, runGatewayWorkspaceListTask, selectionIntentRef, setState],
  );

  return {
    addManagedGatewayWorkspace,
    addSshGatewayWorkspace,
    removeGatewayWorkspace,
    reorderGatewayWorkspaces,
    renameGatewayWorkspace,
  };
}

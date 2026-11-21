import { workspaceOwnerFromKey } from "../../api/workspaceIdentity";
import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { useCallback, useRef, type MutableRefObject } from "react";
import { DEFAULT_BACKEND_PORT } from "../../api";
import {
  addManagedGatewayWorkspace as apiAddManagedGatewayWorkspace,
  addRemoteGatewayConnection as apiAddRemoteGatewayConnection,
  listGatewayWorkspaces as apiListGatewayWorkspaces,
  removeGatewayWorkspace as apiRemoveGatewayWorkspace,
  renameGatewayWorkspace as apiRenameGatewayWorkspace,
  reorderGatewayWorkspaces as apiReorderGatewayWorkspaces,
} from "../../gatewayApi";
import type {
  AddManagedGatewayWorkspaceRequest,
  AddRemoteGatewayConnectionRequest,
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
import { t } from "../../../../shared/core/i18n/index.js";
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
          activeWorkspaceChanged = nullableGatewayWorkspaceKey(workspaces.active_workspace) !== activeWorkspaceIdAtStart;
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
              refreshError = t("gateway.workspace.refreshSuperseded");
            } else if (expectedListRequest === listRequest) {
              refreshError = t("gateway.workspace.refreshNoNewListGeneration");
            } else if (expectedListRequest !== gatewayListRequestRef.current
              || selectionIntent !== selectionIntentRef.current) {
              refreshError = t("gateway.workspace.refreshResultReplaced");
            } else {
              refreshApplied = true;
            }
          } catch (error) {
            refreshError = t("gateway.workspace.refreshWorkspaceListFailed", errorMessage(error));
          }
        } else {
          refreshError = t("gateway.workspace.activeWorkspaceChangedButRefreshSkipped");
        }
      }

      if (postFailed) {
        const details = [
          errorMessage(postError),
          ...(listReadFailed
            ? [t("gateway.workspace.readGatewayWorkspaceListFailed", errorMessage(listReadError))]
            : []),
          ...(refreshError ? [refreshError] : []),
        ];
        const message = details.join(t("gateway.workspace.blockerSeparator"));
        const scopedFailure = t("gateway.workspace.addWorkspacePathFailed", payload.root_path, message);
        setState((prev) => {
          if (expectedListRequest !== gatewayListRequestRef.current
            || selectionIntent !== selectionIntentRef.current) {
            return {
              ...prev,
              status: prev.status ? `${prev.status}${t("gateway.workspace.blockerSeparator")}${scopedFailure}` : scopedFailure,
            };
          }
          return {
            ...prev,
            workspaceSwitching: false,
            gatewayError: message,
            status: t("gateway.workspace.addWorkspaceFailed", message),
            isBootstrapping: false,
          };
        });
        throw postError;
      }

      const reconciliationErrors: string[] = [];
      if (listReadFailed) {
        reconciliationErrors.push(t("gateway.workspace.readGatewayWorkspaceListFailed", errorMessage(listReadError)));
      }
      if (listRequest === gatewayListRequestRef.current
        && selectionIntent === selectionIntentRef.current) {
        try {
          const refresh = finishWorkspaceRefresh(undefined, { selectionIntent });
          expectedListRequest = gatewayListRequestRef.current;
          const appliedWorkspaceId = await refresh;
          if (appliedWorkspaceId === null) {
            reconciliationErrors.push(
              t("gateway.workspace.refreshSuperseded"),
            );
          } else if (expectedListRequest === listRequest) {
            reconciliationErrors.push(t("gateway.workspace.refreshNoNewListGeneration"));
          } else if (expectedListRequest !== gatewayListRequestRef.current
            || selectionIntent !== selectionIntentRef.current) {
            reconciliationErrors.push(t("gateway.workspace.refreshResultReplaced"));
          } else {
            refreshApplied = true;
          }
        } catch (error) {
          reconciliationErrors.push(t("gateway.workspace.refreshWorkspaceListFailed", errorMessage(error)));
        }
      } else if (listReadFailed) {
        reconciliationErrors.push(
          t("gateway.workspace.refreshSkippedAfterSelectionChanged"),
        );
      }

      if (listReadFailed && refreshApplied) {
        reconciliationErrors.shift();
      }
      if (reconciliationErrors.length > 0) {
        const details = reconciliationErrors.join(t("gateway.workspace.blockerSeparator"));
        const message = t("gateway.workspace.workspaceAddedButSyncFailed", details);
        const scopedFailure = t("gateway.workspace.workspacePathAddedButSyncFailed", payload.root_path, details);
        setState((prev) => {
          if (expectedListRequest !== gatewayListRequestRef.current
            || selectionIntent !== selectionIntentRef.current) {
            return {
              ...prev,
              status: prev.status ? `${prev.status}${t("gateway.workspace.blockerSeparator")}${scopedFailure}` : scopedFailure,
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

  const addRemoteGatewayConnection = useCallback(
    async (payload: AddRemoteGatewayConnectionRequest) => {
      const selectionIntent = selectionIntentRef.current;
      const listRequest = ++gatewayListRequestRef.current;
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      resetWorkspaceScopedState();
      const requestIsCurrent = () =>
        listRequest === gatewayListRequestRef.current
        && selectionIntent === selectionIntentRef.current;
      await runGatewayWorkspaceListTask(async () => {
        try {
          return await apiAddRemoteGatewayConnection(resolvedApiPort, payload);
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
              message += t("gateway.workspace.readGatewayWorkspaceListFailedSuffix", errorMessage(reconciliationError));
              setState((prev) => requestIsCurrent()
                ? { ...prev, gatewayWorkspacesStale: true }
                : prev);
            }
          }
          setState((prev) => requestIsCurrent() ? ({
            ...prev,
            workspaceSwitching: false,
            gatewayError: message,
            status: t("gateway.workspace.connectRemoteGatewayFailed", message),
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
        const message = t("gateway.workspace.remoteGatewayConnectedButSyncFailed", detail);
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
        workspaceId, ...(groupRequest?.expected_workspace_ids ?? []).map(id => gatewayWorkspaceKey({ gatewayId: workspaceOwnerFromKey(workspaceId).gatewayId, workspaceId: id })),
        ...(groupRequest?.desired_workspace_ids ?? []).map(id => gatewayWorkspaceKey({ gatewayId: workspaceOwnerFromKey(workspaceId).gatewayId, workspaceId: id })),
      ]);
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      const removedActiveWorkspace = workspaceId === activeGatewayWorkspaceId;
      let workspaceRemoved = false;
      let postCommitFailureStage: string | null = null;
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
        status: t("gateway.workspace.deletingWorkspace"),
      }) : prev);
      try {
        await runGatewayWorkspaceListTask(() => apiRemoveGatewayWorkspace(
          resolvedApiPort,
          workspaceId,
          groupRequest,
          force,
        ), (workspaceList) => {
        workspaceRemoved = true;
        postCommitFailureStage = t("gateway.workspace.deleteResponseHandling");
        if (!listRequestIsCurrent()) return;
        activeWorkspaceChanged =
          nullableGatewayWorkspaceKey(workspaceList.active_workspace) !== activeGatewayWorkspaceId;
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
            sessions: nullableGatewayWorkspaceKey(workspaceList.active_workspace)
              ? reconciledState.sessionsByWorkspace.get(
                  nullableGatewayWorkspaceKey(workspaceList.active_workspace)!,
                ) ?? []
              : [],
            currentSession: null,
            currentSessionWorkspaceId: null,
            llmRequestLogs: [],
            sessionResources: [],
            agentStateJsonl: "",
            agentStateMessageCount: 0,
          };
        });
        });
        postCommitFailureStage = t("gateway.workspace.localUiSettingsCleanup");
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
          postCommitFailureStage = t("gateway.workspace.newActiveWorkspaceLoading");
          const refresh = finishWorkspaceRefresh(undefined, { selectionIntent });
          // finishWorkspaceRefresh 自己推进刷新代次；后续删除对账只能沿用它取得的代次。
          reconciliationGeneration = gatewayListRequestRef.current;
          await refresh;
          postCommitFailureStage = null;
        }
      } catch (error) {
        const workspaceFailureMessage = errorMessage(error);
        const postCommitStage = postCommitFailureStage ?? t("gateway.workspace.deleteResponseHandling");
        const operationMessage = workspaceRemoved
          ? t("gateway.workspace.workspaceDeletedButStageFailed", postCommitStage, workspaceFailureMessage)
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
          ? t("gateway.workspace.operationAndListReadFailed", operationMessage, reconciliationMessage)
          : operationMessage;
        const scopedFailure = workspaceRemoved
          ? t(
            "gateway.workspace.workspaceDeleteCommittedButStageFailed",
            workspaceId,
            postCommitStage,
            workspaceFailureMessage,
            reconciliationMessage
              ? t("gateway.workspace.readWorkspaceListAlsoFailedSuffix", reconciliationMessage)
              : "",
          )
          : t("gateway.workspace.deleteWorkspaceFailedForId", workspaceId, message);
        setState((prev) => {
          if (reconciliationIsCurrent() && selectionIntent === selectionIntentRef.current) {
            return {
              ...prev,
              workspaceSwitching: false,
              gatewayError: message,
              status: workspaceRemoved ? message : t("gateway.workspace.deleteWorkspaceFailed", message),
              isBootstrapping: false,
            };
          }
          return {
            ...prev,
            status: prev.status ? `${prev.status}${t("gateway.workspace.blockerSeparator")}${scopedFailure}` : scopedFailure,
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
            workspaces: workspaceIds.map(key => { const owner = workspaceOwnerFromKey(key); return { gateway_id: owner.gatewayId, workspace_id: owner.workspaceId }; }),
          });
          setState((prev) => listRequest === gatewayListRequestRef.current ? {
            ...withFreshGatewayWorkspaceList(prev, workspaceList.items),
            ...(selectionIntent === selectionIntentRef.current ? {
              ...activeGatewayWorkspaceFields(workspaceList), status: t("gateway.workspace.workspaceOrderUpdated"),
            } : {}),
          } : prev);
        } catch (error) {
          const operationMessage = errorMessage(error);
          let message = operationMessage;
          let workspaceList: Awaited<ReturnType<typeof apiListGatewayWorkspaces>> | null = null;
          try {
            workspaceList = await apiListGatewayWorkspaces(resolvedApiPort);
          } catch (reconciliationError) {
            message += t("gateway.workspace.readWorkspaceListAlsoFailedSuffix", errorMessage(reconciliationError));
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
              status: t("gateway.workspace.reorderWorkspacesFailed", message),
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
            (workspace) => gatewayWorkspaceKey(workspace) === workspaceId,
          );
          if (!renamedWorkspace) {
            throw new Error(t("gateway.workspace.renameResponseMissingWorkspace", workspaceId));
          }
          setState((prev) => {
            if (listRequest !== gatewayListRequestRef.current) return prev;
            return {
              ...withFreshGatewayWorkspaceList(prev, workspaceList.items),
              ...(selectionIntent === selectionIntentRef.current ? {
                ...activeGatewayWorkspaceFields(workspaceList), gatewayError: null, error: null,
                status: t("gateway.workspace.workspaceRenamed", renamedWorkspace.name),
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
            message = t("gateway.workspace.operationAndListReadFailed", operationMessage, reconciliationMessage);
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
                status: t("gateway.workspace.renameWorkspaceFailed", message),
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
    addRemoteGatewayConnection,
    removeGatewayWorkspace,
    reorderGatewayWorkspaces,
    renameGatewayWorkspace,
  };
}

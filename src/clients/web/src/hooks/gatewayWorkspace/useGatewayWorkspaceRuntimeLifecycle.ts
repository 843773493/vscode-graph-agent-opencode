import { workspaceOwnerFromKey } from "../../api/workspaceIdentity";
import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey } from "../../api/workspaceIdentity";
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
import { t } from "../../../../shared/core/i18n/index.js";

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
      status: t("gateway.workspace.reconnectingWorkspace"),
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
      const activeWorkspaceIsListed = nullableGatewayWorkspaceKey(workspaces.active_workspace) === null
        || workspaces.items.some((item) => gatewayWorkspaceKey(item) === nullableGatewayWorkspaceKey(workspaces.active_workspace));
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
      const message = t("gateway.workspace.operationAndListReadFailed", errorMessage(outcome.postError), errorMessage(outcome.readbackError));
      setState((prev) => {
        if (!requestIsCurrent()) {
          const scopedFailure = t("gateway.workspace.reconnectFailed", message);
          return { ...prev, status: prev.status ? `${prev.status}${t("gateway.workspace.blockerSeparator")}${scopedFailure}` : scopedFailure };
        }
        return {
          ...prev,
          gatewayWorkspacesStale: true,
          workspaceSwitching: false,
          gatewayError: message,
          status: t("gateway.workspace.reconnectFailed", message),
        };
      });
      throw outcome.postError;
    }

    const { workspaces } = outcome;
    const activeWorkspaceIsListed = nullableGatewayWorkspaceKey(workspaces.active_workspace) === null
      || workspaces.items.some((item) => gatewayWorkspaceKey(item) === nullableGatewayWorkspaceKey(workspaces.active_workspace));
    if (!activeWorkspaceIsListed) {
      const detail = t("gateway.workspace.activeWorkspaceMissingFromList", nullableGatewayWorkspaceKey(workspaces.active_workspace));
      if (outcome.kind === "readback") {
        const message = t("gateway.workspace.operationAndListReadFailed", errorMessage(outcome.postError), detail);
        setState((prev) => {
          if (!requestIsCurrent()) {
            const scopedFailure = t("gateway.workspace.reconnectFailed", message);
            return { ...prev, status: prev.status ? `${prev.status}${t("gateway.workspace.blockerSeparator")}${scopedFailure}` : scopedFailure };
          }
          return {
            ...prev,
            gatewayWorkspacesStale: true,
            workspaceSwitching: false,
            gatewayError: message,
            status: t("gateway.workspace.reconnectFailed", message),
          };
        });
        throw outcome.postError;
      }
      const message = t("gateway.workspace.reconnectedButSyncFailed", detail);
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
        throw new Error(t("gateway.workspace.refreshSuperseded"));
      }
      if (appliedWorkspaceId !== nullableGatewayWorkspaceKey(workspaces.active_workspace)) {
        throw new Error(t("gateway.workspace.refreshActiveWorkspaceMismatch", appliedWorkspaceId));
      }
      if (expectedListRequest !== gatewayListRequestRef.current || !selectionIsCurrent()) {
        throw new Error(t("gateway.workspace.refreshResultReplaced"));
      }
    } catch (error) {
      finishError = error;
      finishFailed = true;
    }

    if (finishFailed) {
      const detail = errorMessage(finishError);
      if (outcome.kind === "readback") {
        const message = t("gateway.workspace.operationAndSyncFailed", errorMessage(outcome.postError), detail);
        setState((prev) => {
          if (expectedListRequest !== gatewayListRequestRef.current || !selectionIsCurrent()) {
            const scopedFailure = t("gateway.workspace.reconnectFailed", message);
            return { ...prev, status: prev.status ? `${prev.status}${t("gateway.workspace.blockerSeparator")}${scopedFailure}` : scopedFailure };
          }
          return {
            ...prev,
            workspaceSwitching: false,
            gatewayError: message,
            status: t("gateway.workspace.reconnectFailed", message),
          };
        });
        throw outcome.postError;
      }
      const message = t("gateway.workspace.reconnectedButSyncFailed", detail);
      setState((prev) => {
        if (expectedListRequest !== gatewayListRequestRef.current || !selectionIsCurrent()) {
          return { ...prev, status: prev.status ? `${prev.status}${t("gateway.workspace.blockerSeparator")}${message}` : message };
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
      const message = t("gateway.workspace.reconnectRejectedButLatestStateRead", errorMessage(outcome.postError));
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
        ...(groupRequest?.expected_workspace_ids ?? []).map(id => gatewayWorkspaceKey({ gatewayId: workspaceOwnerFromKey(workspaceId).gatewayId, workspaceId: id })),
        ...(groupRequest?.desired_workspace_ids ?? []).map(id => gatewayWorkspaceKey({ gatewayId: workspaceOwnerFromKey(workspaceId).gatewayId, workspaceId: id })),
      ]);
      try {
        const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
        setState((prev) => ({
          ...prev,
          gatewayError: null,
          status: t("gateway.workspace.safelyDrainingAndRestartingBackend"),
        }));
        let result: GatewayRuntimeRestartResult;
        try {
          result = await runGatewayWorkspaceListTask(() => apiSafeRestartManagedGatewayWorkspaceBackend(
            resolvedApiPort,
            workspaceId,
            groupRequest,
          ), (result) => {
            const actionStatus = result.status === "blocked"
              ? result.blockers.map((blocker) => t("gateway.workspace.runtimeBlocker", blocker.workspace_id, blocker.detail ?? blocker.resource_id)).join(t("gateway.workspace.blockerSeparator"))
              : result.status === "stopped" ? t("gateway.workspace.backendGroupStoppedExited") : t("gateway.workspace.backendGroupRestarted");
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
            status: t("gateway.workspace.safeRestartBackendFailed", message),
          }) : prev);
          throw error;
        }

        const actionStatus = result.status === "blocked"
          ? result.blockers.map((blocker) => t("gateway.workspace.runtimeBlocker", blocker.workspace_id, blocker.detail ?? blocker.resource_id)).join(t("gateway.workspace.blockerSeparator"))
          : result.status === "stopped" ? t("gateway.workspace.backendGroupStoppedExited") : t("gateway.workspace.backendGroupRestarted");
        try {
          await finishWorkspaceRefresh(currentSessionId, { selectionIntent });
        } catch (error) {
          try {
            await refreshGatewayState(selectionIntent);
          } catch {
            // 二次读取失败已由 refreshGatewayState 写入界面。
          }
          const message = t("gateway.workspace.actionAndStatusRefreshFailed", actionStatus || t("gateway.workspace.backendRestartBlocked"), errorMessage(error));
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
        ...(groupRequest?.expected_workspace_ids ?? []).map(id => gatewayWorkspaceKey({ gatewayId: workspaceOwnerFromKey(workspaceId).gatewayId, workspaceId: id })),
        ...(groupRequest?.desired_workspace_ids ?? []).map(id => gatewayWorkspaceKey({ gatewayId: workspaceOwnerFromKey(workspaceId).gatewayId, workspaceId: id })),
      ]);
      try {
        const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
        setState((prev) => ({ ...prev, gatewayError: null, status: t("gateway.workspace.startingWorkspace") }));
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
                status: result.status === "started" ? t("gateway.workspace.backendGroupStarted") : t("gateway.workspace.backendGroupNotStarted"),
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
            status: t("gateway.workspace.startWorkspaceFailed", message),
          }) : prev);
          throw error;
        }
        if (result.status === "blocked") {
          const message = result.blockers.map((blocker) => t("gateway.workspace.runtimeBlocker", blocker.workspace_id, blocker.detail ?? blocker.resource_id)).join(t("gateway.workspace.blockerSeparator"));
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({ ...prev, gatewayError: message, status: message }) : prev);
          throw new Error(message);
        }
        try {
          await finishWorkspaceRefresh(currentSessionId, { selectionIntent });
        } catch (error) {
          const message = t("gateway.workspace.workspaceStartedButStatusRefreshFailed", errorMessage(error));
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
        ...(groupRequest?.expected_workspace_ids ?? []).map(id => gatewayWorkspaceKey({ gatewayId: workspaceOwnerFromKey(workspaceId).gatewayId, workspaceId: id })),
        ...(groupRequest?.desired_workspace_ids ?? []).map(id => gatewayWorkspaceKey({ gatewayId: workspaceOwnerFromKey(workspaceId).gatewayId, workspaceId: id })),
      ]);
      try {
        const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
        setState((prev) => ({ ...prev, gatewayError: null, status: t("gateway.workspace.stoppingWorkspace") }));
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
                status: result.status === "blocked" ? t("gateway.workspace.workspaceStillHasActiveTasks") : t("gateway.workspace.workspaceStopped"),
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
            status: t("gateway.workspace.stopWorkspaceFailed", message),
          }) : prev);
          throw error;
        }

        if (result.status === "blocked") {
          const details = result.blockers
            .map((blocker) => t("gateway.workspace.runtimeBlockerWithKind", blocker.workspace_id, blocker.kind, blocker.resource_id))
            .join(t("gateway.workspace.blockerSeparator"));
          const error = new Error(details
            ? t("gateway.workspace.workspaceHasActiveTasksWithDetails", result.blockers.length, details)
            : t("gateway.workspace.workspaceHasActiveTasks", result.blockers.length));
          try {
            await refreshGatewayState(selectionIntent);
          } catch {
            // 二次读取失败已由 refreshGatewayState 写入界面状态。
          }
          const message = errorMessage(error);
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({
            ...prev,
            gatewayError: message,
            status: t("gateway.workspace.stopWorkspaceFailed", message),
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
          const message = t("gateway.workspace.workspaceStoppedButStatusRefreshFailed", errorMessage(error));
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
        ...(groupRequest?.expected_workspace_ids ?? []).map(id => gatewayWorkspaceKey({ gatewayId: workspaceOwnerFromKey(workspaceId).gatewayId, workspaceId: id })),
        ...(groupRequest?.desired_workspace_ids ?? []).map(id => gatewayWorkspaceKey({ gatewayId: workspaceOwnerFromKey(workspaceId).gatewayId, workspaceId: id })),
      ]);
      try {
        const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
        setState((prev) => ({
          ...prev,
          gatewayError: null,
          status: t("gateway.workspace.interruptingAndForceRestartingBackend"),
        }));
        let result: GatewayRuntimeRestartResult;
        try {
          result = await runGatewayWorkspaceListTask(() => apiForceRestartManagedGatewayWorkspaceBackend(
            resolvedApiPort,
            workspaceId,
            groupRequest,
          ), (result) => {
            const actionStatus = result.status === "blocked"
              ? result.blockers.map((blocker) => t("gateway.workspace.runtimeBlocker", blocker.workspace_id, blocker.detail ?? blocker.resource_id)).join(t("gateway.workspace.blockerSeparator"))
              : result.status === "stopped" ? t("gateway.workspace.backendGroupStoppedExited") : t("gateway.workspace.backendGroupRestarted");
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
            status: t("gateway.workspace.forceRestartBackendFailed", message),
          }) : prev);
          throw error;
        }

        const actionStatus = result.status === "blocked"
          ? result.blockers.map((blocker) => t("gateway.workspace.runtimeBlocker", blocker.workspace_id, blocker.detail ?? blocker.resource_id)).join(t("gateway.workspace.blockerSeparator"))
          : result.status === "stopped" ? t("gateway.workspace.backendGroupStoppedExited") : t("gateway.workspace.backendGroupRestarted");
        try {
          await finishWorkspaceRefresh(currentSessionId, { selectionIntent });
        } catch (error) {
          try {
            await refreshGatewayState(selectionIntent);
          } catch {
            // 二次读取失败已由 refreshGatewayState 写入界面。
          }
          const message = result.status === "blocked"
            ? t("gateway.workspace.actionAndStatusRefreshFailed", actionStatus || t("gateway.workspace.backendRestartBlocked"), errorMessage(error))
            : t("gateway.workspace.actionButStatusRefreshFailed", actionStatus, errorMessage(error));
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
        status: t("gateway.workspace.probingExternalBackend"),
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
          status: t("gateway.workspace.probeExternalBackendFailed", message),
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

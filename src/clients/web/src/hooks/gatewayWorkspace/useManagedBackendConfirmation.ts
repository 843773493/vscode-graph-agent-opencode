import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { useCallback } from "react";
import { listGatewayWorkspaces } from "../../gatewayApi";
import { useWarmConfirm } from "../../components/shell/WarmConfirmProvider";
import type { GatewayRuntimeGroupRequest, GatewayRuntimeRestartResult } from "../../types/backend";
import { t } from "../../../../shared/core/i18n/index.js";

type StateAction = (workspaceId: string, request?: GatewayRuntimeGroupRequest) => Promise<void>;
type RestartAction = (workspaceId: string, request?: GatewayRuntimeGroupRequest) => Promise<GatewayRuntimeRestartResult>;
type Action = "start" | "stop" | "restart-safe" | "restart-force";

export function useManagedBackendConfirmation({
  apiPort, onStart, onStop, onSafeRestart, onForceRestart, onRefresh,
}: {
  apiPort: number;
  onStart: StateAction;
  onStop: StateAction;
  onSafeRestart: RestartAction;
  onForceRestart: RestartAction;
  onRefresh: () => Promise<void>;
}) {
  const confirm = useWarmConfirm();
  const execute = useCallback(async (
    workspaceId: string, action: Action, selectedRequest?: GatewayRuntimeGroupRequest,
  ): Promise<void> => {
    try {
      const snapshot = await listGatewayWorkspaces(apiPort);
      const target = snapshot.items.find((item) => gatewayWorkspaceKey(item) === workspaceId);
      if (!target?.managed) throw new Error(t("gateway.workspace.notManagedTarget"));
      const members = target.backend_generation ? target.affected_workspace_ids : [target.workspace_id];
      if (!members.length || !members.includes(target.workspace_id)) throw new Error(t("gateway.workspace.backendMemberContractMissing"));
      if (selectedRequest && (
        selectedRequest.expected_backend_generation !== target.backend_generation
        || selectedRequest.expected_workspace_ids.length !== members.length
        || selectedRequest.expected_workspace_ids.some((id) => !members.includes(id))
      )) throw new Error(t("gateway.workspace.backendMembersChanged"));
      const affected = [...new Set([...members, ...(selectedRequest?.desired_workspace_ids ?? [])])];
      const names = affected.map((id) => {
        const member = snapshot.items.find((item) => item.gateway_id === target.gateway_id && item.workspace_id === id);
        if (!member) throw new Error(t("gateway.workspace.backendMemberNotRegistered", id));
        return t("gateway.workspace.backendMemberBullet", member.name, id);
      });
      const exiting = selectedRequest?.desired_workspace_ids?.length === 0;
      const actionLabels = {
        start: t("gateway.workspace.startAction"),
        stop: t("gateway.workspace.stopAction"),
        "restart-safe": t("gateway.workspace.safeRestartAction"),
        "restart-force": t("gateway.workspace.forceRestartAction"),
      };
      const label = exiting ? t("gateway.workspace.closeAndExit") : actionLabels[action];
      if (members.length > 1 || action !== "start" || selectedRequest?.desired_workspace_ids !== undefined) {
        const detail = action === "restart-force"
          ? exiting
            ? t("gateway.workspace.forceRestartAndExitDetail")
            : t("gateway.workspace.forceRestartDetail")
          : action === "stop"
            ? t("gateway.workspace.stopBackendDetail")
            : exiting
              ? t("gateway.workspace.drainAndExitDetail")
              : t("gateway.workspace.startBackendDetail");
        const accepted = await confirm({
          title: t("gateway.workspace.confirmActionTitle", label),
          message: t("gateway.workspace.confirmActionMessage", label, names.join("\n"), detail),
          confirmText: label,
          danger: action === "stop" || action === "restart-force",
        });
        if (!accepted) return;
      }
      const request = target.backend_generation ? {
        expected_backend_generation: target.backend_generation,
        expected_workspace_ids: members,
        ...(selectedRequest?.desired_workspace_ids !== undefined ? { desired_workspace_ids: selectedRequest.desired_workspace_ids } : {}),
      } : undefined;
      if (action === "start") await onStart(workspaceId, request);
      else if (action === "stop") await onStop(workspaceId, request);
      else {
        const result = await (action === "restart-force" ? onForceRestart : onSafeRestart)(workspaceId, request);
        if (result.status === "blocked") throw new Error(result.blockers
          .map((blocker) => t("gateway.workspace.runtimeBlocker", blocker.workspace_id, blocker.detail ?? blocker.resource_id)).join(t("gateway.workspace.blockerSeparator")));
      }
    } catch (error) {
      await onRefresh().catch(() => { /* 刷新错误由状态层显式呈现，首个操作错误继续抛出。 */ });
      throw error;
    }
  }, [apiPort, confirm, onStart, onStop, onSafeRestart, onForceRestart, onRefresh]);
  return {
    start: useCallback((id: string) => execute(id, "start"), [execute]),
    stop: useCallback((id: string) => execute(id, "stop"), [execute]),
    safeRestart: useCallback((id: string, request?: GatewayRuntimeGroupRequest) => execute(id, "restart-safe", request), [execute]),
    forceRestart: useCallback((id: string, request?: GatewayRuntimeGroupRequest) => execute(id, "restart-force", request), [execute]),
  };
}

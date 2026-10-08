import { useCallback } from "react";
import { listGatewayWorkspaces } from "../../gatewayApi";
import { useWarmConfirm } from "../../components/shell/WarmConfirmProvider";
import type { GatewayRuntimeGroupRequest, GatewayRuntimeRestartResult } from "../../types/backend";

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
      const target = snapshot.items.find((item) => item.workspace_id === workspaceId);
      if (!target?.managed) throw new Error("工作区未登记为托管目标，请刷新 Gateway");
      const members = target.backend_generation ? target.affected_workspace_ids : [workspaceId];
      if (!members.length || !members.includes(workspaceId)) throw new Error("Backend 完整成员合同缺失");
      if (selectedRequest && (
        selectedRequest.expected_backend_generation !== target.backend_generation
        || selectedRequest.expected_workspace_ids.length !== members.length
        || selectedRequest.expected_workspace_ids.some((id) => !members.includes(id))
      )) throw new Error("Backend 成员或 generation 已变化，请刷新后重新选择");
      const affected = [...new Set([...members, ...(selectedRequest?.desired_workspace_ids ?? [])])];
      const names = affected.map((id) => {
        const member = snapshot.items.find((item) => item.workspace_id === id);
        if (!member) throw new Error(`Backend 成员未登记：${id}`);
        return `• ${member.name}（${id}）`;
      });
      const exiting = selectedRequest?.desired_workspace_ids?.length === 0;
      const label = exiting ? "关闭并退出" : { start: "启动", stop: "关闭", "restart-safe": "安全重启", "restart-force": "强制重启" }[action];
      if (members.length > 1 || action !== "start" || selectedRequest?.desired_workspace_ids !== undefined) {
        const accepted = await confirm({
          title: `${label} Backend 完整组`,
          message: `以下工作区将一起${label}：\n${names.join("\n")}\n\n${action === "restart-force"
            ? exiting ? "全部成员中的活动任务将先记录为 interrupted，然后关闭 Backend 并退出。" : "全部成员中的活动任务将先记录为 interrupted，然后重启。"
            : action === "stop" ? "整个组会暂时离线；活动任务需要先排空。"
            : exiting ? "排空后将关闭 Backend 并释放全部成员；不会启动空 Backend。"
            : "操作会核对最新成员合同，并等待全部工作区就绪。"}`,
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
          .map((blocker) => `${blocker.workspace_id}：${blocker.detail ?? blocker.resource_id}`).join("；"));
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

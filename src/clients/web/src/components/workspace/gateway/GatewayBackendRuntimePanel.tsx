import { useMemo, useState } from "react";
import type { GatewayRuntimeGroupRequest, GatewayWorkspace } from "../../../types/backend";
import { errorMessage } from "../../../utils/errorMessage";

export interface GatewayBackendRuntimeActions {
  onStart: (id: string) => Promise<void>;
  onStop: (id: string) => Promise<void>;
  onSafeRestart: (id: string, request?: GatewayRuntimeGroupRequest) => Promise<void>;
  onForceRestart: (id: string, request?: GatewayRuntimeGroupRequest) => Promise<void>;
}

export default function GatewayBackendRuntimePanel({
  workspaces, onStart, onStop, onSafeRestart, onForceRestart,
}: GatewayBackendRuntimeActions & { workspaces: GatewayWorkspace[] }) {
  const [memberSelection, setMemberSelection] = useState<{
    backendGeneration: string;
    targetWorkspaceId: string;
    desiredWorkspaceIds: string[];
  } | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const workspaceById = useMemo(() => {
    const index = new Map<string, GatewayWorkspace>();
    for (const workspace of workspaces) {
      if (!index.has(workspace.workspace_id)) index.set(workspace.workspace_id, workspace);
    }
    return index;
  }, [workspaces]);
  const managedWorkspaces = useMemo(
    () => workspaces.filter((workspace) => workspace.managed),
    [workspaces],
  );
  const runtimeGroups = useMemo(() => {
    const groups = new Map<string, GatewayWorkspace[]>();
    for (const workspace of managedWorkspaces) {
      const groupKey = `${workspace.remote?.gateway_connection_id ?? "local"}:${workspace.backend_generation ?? workspace.workspace_id}`;
      const groupMembers = groups.get(groupKey) ?? [];
      groupMembers.push(workspace);
      groups.set(groupKey, groupMembers);
    }
    return [...groups.entries()].map(([groupKey, groupMembers]) => {
      const groupTarget = groupMembers.find((workspace) => workspace.system_default) ?? groupMembers[0];
      const memberWorkspaceIds = groupTarget.backend_generation
        ? groupTarget.affected_workspace_ids
        : [groupTarget.workspace_id];
      const memberWorkspaceIdSet = new Set(memberWorkspaceIds);
      const memberWorkspaces = memberWorkspaceIds.map((workspaceId) => workspaceById.get(workspaceId));
      const candidateWorkspaces = managedWorkspaces.filter((workspace) =>
        memberWorkspaceIdSet.has(workspace.workspace_id)
        || (workspace.status === "offline" && workspace.affected_workspace_ids.length <= 1
          && workspace.remote?.gateway_connection_id === groupTarget.remote?.gateway_connection_id),
      );
      return {
        groupKey,
        groupTarget,
        memberWorkspaceIds,
        memberWorkspaces,
        allMembersReady: groupMembers.every((workspace) => workspace.status === "ready"),
        hasProtectedMember: memberWorkspaces.some(
          (workspace) => workspace?.system_default || workspace?.removable === false,
        ),
        candidateWorkspaces,
      };
    });
  }, [managedWorkspaces, workspaceById]);
  const runRuntimeAction = async (runtimeAction: () => Promise<void>) => {
    setActionBusy(true);
    setActionError(null);
    try { await runtimeAction(); setMemberSelection(null); }
    catch (failure) { setActionError(errorMessage(failure)); }
    finally { setActionBusy(false); }
  };
  return <section aria-label="Backend 运行时">
    {actionError ? <div className="gateway-console-alert" role="alert">{actionError}</div> : null}
    {runtimeGroups.map((runtimeGroup) => {
      const {
        groupKey,
        groupTarget,
        memberWorkspaceIds,
        memberWorkspaces,
        allMembersReady,
        hasProtectedMember,
        candidateWorkspaces,
      } = runtimeGroup;
      const isMemberSelectionOpen = memberSelection?.backendGeneration === groupTarget.backend_generation
        && memberSelection?.targetWorkspaceId === groupTarget.workspace_id;
      const desiredWorkspaceIds = isMemberSelectionOpen && memberSelection
        ? memberSelection.desiredWorkspaceIds
        : memberWorkspaceIds;
      const groupRestartRequest = groupTarget.backend_generation ? {
        expected_backend_generation: groupTarget.backend_generation,
        expected_workspace_ids: memberWorkspaceIds,
        desired_workspace_ids: desiredWorkspaceIds,
      } : undefined;
      return <article className="gateway-connection-section" key={groupKey}>
        <h2>{memberWorkspaces.map((workspace, index) => workspace?.name ?? memberWorkspaceIds[index]).join("、")}</h2>
        <p>{allMembersReady ? "运行中" : "离线或异常"} · {memberWorkspaceIds.length} 个工作区共用一个 Backend</p>
        <div className="gateway-control-actions">
          <button type="button" disabled={actionBusy || allMembersReady} onClick={() => void runRuntimeAction(() => onStart(groupTarget.workspace_id))}>启动完整组</button>
          <button type="button" disabled={actionBusy || !allMembersReady || hasProtectedMember} onClick={() => void runRuntimeAction(() => onStop(groupTarget.workspace_id))}>关闭完整组</button>
          <button type="button" disabled={actionBusy || !groupTarget.backend_generation} onClick={() => void runRuntimeAction(() => onSafeRestart(groupTarget.workspace_id))}>安全重启</button>
          <button type="button" className="danger-soft" disabled={actionBusy || !groupTarget.backend_generation} onClick={() => void runRuntimeAction(() => onForceRestart(groupTarget.workspace_id))}>强制重启</button>
          <button type="button" disabled={actionBusy || !groupTarget.backend_generation} onClick={() => setMemberSelection(
            isMemberSelectionOpen ? null : {
              backendGeneration: groupTarget.backend_generation!,
              targetWorkspaceId: groupTarget.workspace_id,
              desiredWorkspaceIds: memberWorkspaceIds,
            },
          )}>调整成员</button>
        </div>
        {isMemberSelectionOpen ? <fieldset disabled={actionBusy}>
          <legend>重启后的完整成员</legend>
          {candidateWorkspaces.map((workspace) => <label key={workspace.workspace_id} style={{ display: "block" }}>
            <input type="checkbox" checked={desiredWorkspaceIds.includes(workspace.workspace_id)}
              disabled={workspace.system_default || !workspace.removable}
              onChange={(event) => setMemberSelection({
                backendGeneration: groupTarget.backend_generation!,
                targetWorkspaceId: groupTarget.workspace_id,
                desiredWorkspaceIds: event.target.checked
                  ? [...desiredWorkspaceIds, workspace.workspace_id]
                  : desiredWorkspaceIds.filter((workspaceId) => workspaceId !== workspace.workspace_id),
              })} /> {workspace.name}{workspace.system_default ? "（默认工作区）" : ""}
          </label>)}
          <p>加入前请先关闭另一个 singleton；移出全部成员将关闭 Backend，其余变更会一起受控重启。</p>
          <button type="button" onClick={() => void runRuntimeAction(() => onSafeRestart(groupTarget.workspace_id, groupRestartRequest))}>{desiredWorkspaceIds.length ? "确认成员并安全重启" : "排空并退出全部成员"}</button>
          <button type="button" className="danger-soft" onClick={() => void runRuntimeAction(() => onForceRestart(groupTarget.workspace_id, groupRestartRequest))}>{desiredWorkspaceIds.length ? "确认成员并强制重启" : "中断任务并退出全部成员"}</button>
        </fieldset> : null}
      </article>;
    })}
    {managedWorkspaces.length === 0 ? <p>当前 Gateway 没有托管工作区。</p> : null}
    {actionBusy ? <p role="status">正在操作完整组，请等待所有工作区就绪…</p> : null}
  </section>;
}

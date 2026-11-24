import { gatewayWorkspaceKey } from "../../../api/workspaceIdentity";
import { t } from "../../../../../shared/core/i18n/index.js";
import { useContext, useMemo, useState } from "react";
import type { GatewayRuntimeGroupRequest, GatewayWorkspace } from "../../../types/backend";
import { errorDiagnostic, errorMessage } from "../../../utils/errorMessage";
import { failureSummary } from "../../../../../shared/core/failurePresentation";
import { ComposerDiagnosticsContext } from "../../composer/ComposerDiagnosticsContext";

export interface GatewayBackendRuntimeActions {
  onStart: (id: string) => Promise<void>;
  onStop: (id: string) => Promise<void>;
  onSafeRestart: (id: string, request?: GatewayRuntimeGroupRequest) => Promise<void>;
  onForceRestart: (id: string, request?: GatewayRuntimeGroupRequest) => Promise<void>;
}

export default function GatewayBackendRuntimePanel({
  workspaces, workspaceKey, onStart, onStop, onSafeRestart, onForceRestart,
}: GatewayBackendRuntimeActions & { workspaces: GatewayWorkspace[]; workspaceKey?: string }) {
  const diagnostics = useContext(ComposerDiagnosticsContext);
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
      index.set(gatewayWorkspaceKey(workspace), workspace);
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
      const groupKey = JSON.stringify([workspace.gateway_id, workspace.backend_generation ?? workspace.workspace_id]);
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
      const memberWorkspaces = memberWorkspaceIds.map((workspaceId) => workspaceById.get(gatewayWorkspaceKey({ gateway_id: groupTarget.gateway_id, workspace_id: workspaceId })));
      const candidateWorkspaces = managedWorkspaces.filter((workspace) =>
        workspace.gateway_id === groupTarget.gateway_id && (memberWorkspaceIdSet.has(workspace.workspace_id)
        || (workspace.status === "offline" && workspace.affected_workspace_ids.length <= 1)),
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
  const visibleGroups = runtimeGroups.filter(group => !workspaceKey || group.memberWorkspaceIds.some(id =>
    gatewayWorkspaceKey({ gateway_id: group.groupTarget.gateway_id, workspace_id: id }) === workspaceKey));
  const runRuntimeAction = async (target: GatewayWorkspace, runtimeAction: () => Promise<void>) => {
    setActionBusy(true);
    setActionError(null);
    try { await runtimeAction(); setMemberSelection(null); }
    catch (failure) {
      setActionError(errorMessage(failure));
      diagnostics(`Gateway=${target.gateway_id} workspace=${target.workspace_id}\n${errorDiagnostic(failure)}`);
    }
    finally { setActionBusy(false); }
  };
  return <section aria-label={t("gateway.runtime.section")}>
    <p>{t("gateway.runtime.lifecycleImpact")}</p>
    {actionError ? <div className="gateway-console-alert failure-summary" role="alert">{failureSummary(actionError)}</div> : null}
    {visibleGroups.map((runtimeGroup) => {
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
        && memberSelection?.targetWorkspaceId === gatewayWorkspaceKey(groupTarget);
      const desiredWorkspaceIds = isMemberSelectionOpen && memberSelection
        ? memberSelection.desiredWorkspaceIds
        : memberWorkspaceIds;
      const groupRestartRequest = groupTarget.backend_generation ? {
        expected_backend_generation: groupTarget.backend_generation,
        expected_workspace_ids: memberWorkspaceIds,
        desired_workspace_ids: desiredWorkspaceIds,
      } : undefined;
      return <article className="gateway-connection-section" key={groupKey}>
        <h2>{memberWorkspaces.map((workspace, index) => workspace?.name ?? memberWorkspaceIds[index]).join(t("gateway.runtime.memberSeparator"))}</h2>
        <p>{allMembersReady ? t("gateway.runtime.running") : t("gateway.runtime.offlineOrError")} · {t("gateway.runtime.sharedWorkspaceCount", memberWorkspaceIds.length)}</p>
        <p>{t("gateway.runtime.owner")} <code>{groupTarget.gateway_id}</code> · {t("gateway.runtime.generation")} <code>{groupTarget.backend_generation ?? t("gateway.runtime.notStarted")}</code></p>
        <ul aria-label={t("gateway.runtime.currentMembers")}>{memberWorkspaceIds.map((id, index) => <li key={id}>
          <strong>{memberWorkspaces[index]?.name ?? id}</strong> <code>{id}</code>
        </li>)}</ul>
        {hasProtectedMember ? <p>{t("gateway.runtime.protectedMember")}</p> : null}
        <div className="gateway-control-actions">
          <button type="button" disabled={actionBusy || allMembersReady} onClick={() => void runRuntimeAction(groupTarget, () => onStart(gatewayWorkspaceKey(groupTarget)))}>{t("gateway.runtime.startGroup")}</button>
          <button type="button" disabled={actionBusy || !allMembersReady || hasProtectedMember} onClick={() => void runRuntimeAction(groupTarget, () => onStop(gatewayWorkspaceKey(groupTarget)))}>{t("gateway.runtime.stopGroup")}</button>
          <button type="button" disabled={actionBusy || !groupTarget.backend_generation} onClick={() => void runRuntimeAction(groupTarget, () => onSafeRestart(gatewayWorkspaceKey(groupTarget)))}>{t("gateway.runtime.safeRestart")}</button>
          <button type="button" className="danger-soft" disabled={actionBusy || !groupTarget.backend_generation} onClick={() => void runRuntimeAction(groupTarget, () => onForceRestart(gatewayWorkspaceKey(groupTarget)))}>{t("gateway.runtime.forceRestart")}</button>
          <button type="button" disabled={actionBusy || !groupTarget.backend_generation} onClick={() => setMemberSelection(
            isMemberSelectionOpen ? null : {
              backendGeneration: groupTarget.backend_generation!,
              targetWorkspaceId: gatewayWorkspaceKey(groupTarget),
              desiredWorkspaceIds: memberWorkspaceIds,
            },
          )}>{t("gateway.runtime.adjustMembers")}</button>
        </div>
        {isMemberSelectionOpen ? <fieldset disabled={actionBusy}>
          <legend>{t("gateway.runtime.membersAfterRestart")}</legend>
          {candidateWorkspaces.map((workspace) => <label key={gatewayWorkspaceKey(workspace)} style={{ display: "block" }}>
            <input type="checkbox" checked={desiredWorkspaceIds.includes(workspace.workspace_id)}
              disabled={workspace.system_default || !workspace.removable}
              onChange={(event) => setMemberSelection({
                backendGeneration: groupTarget.backend_generation!,
                targetWorkspaceId: gatewayWorkspaceKey(groupTarget),
                desiredWorkspaceIds: event.target.checked
                  ? [...desiredWorkspaceIds, workspace.workspace_id]
                  : desiredWorkspaceIds.filter((workspaceId) => workspaceId !== workspace.workspace_id),
              })} /> {workspace.name}{workspace.system_default ? t("gateway.runtime.defaultWorkspaceSuffix") : ""}
          </label>)}
          <p>{t("gateway.runtime.memberChangeDetail")}</p>
          <button type="button" onClick={() => void runRuntimeAction(groupTarget, () => onSafeRestart(gatewayWorkspaceKey(groupTarget), groupRestartRequest))}>{desiredWorkspaceIds.length ? t("gateway.runtime.confirmMembersSafeRestart") : t("gateway.runtime.drainAndExitAll")}</button>
          <button type="button" className="danger-soft" onClick={() => void runRuntimeAction(groupTarget, () => onForceRestart(gatewayWorkspaceKey(groupTarget), groupRestartRequest))}>{desiredWorkspaceIds.length ? t("gateway.runtime.confirmMembersForceRestart") : t("gateway.runtime.interruptAndExitAll")}</button>
        </fieldset> : null}
      </article>;
    })}
    {visibleGroups.length === 0 ? <p>{t("gateway.runtime.noManagedWorkspaces")}</p> : null}
    {actionBusy ? <p role="status">{t("gateway.runtime.waitForGroup")}</p> : null}
  </section>;
}

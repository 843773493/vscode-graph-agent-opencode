import type { GatewayWorkspace } from "../../../types/backend";
import { t } from "../../../../../shared/core/i18n/index.js";

export interface WorkspaceAddGatewayChoice {
  key: string;
  connectionId: string | null;
  kind: "local" | "remote";
  name: string;
  detail: string;
  workspaceCount: number;
  status: "ready" | "offline";
  statusLabel: string;
  error: string | null;
}

export function buildWorkspaceAddGatewayChoices(
  workspaces: GatewayWorkspace[],
): WorkspaceAddGatewayChoice[] {
  let localWorkspaceCount = 0;
  const remoteGroups = new Map<string, {
    remote: GatewayWorkspace["remote_gateways"][number];
    workspaceCount: number;
    hasReadyWorkspace: boolean;
    connectionError: string | null;
  }>();
  for (const workspace of workspaces) {
    if (workspace.connection_kind === "local") localWorkspaceCount += 1;
    for (const remote of workspace.remote_gateways) {
      const connectionId = remote.gateway_connection_id;
      let group = remoteGroups.get(connectionId);
      if (!group) {
        group = { remote, workspaceCount: 0, hasReadyWorkspace: false, connectionError: null };
        remoteGroups.set(connectionId, group);
      }
      group.workspaceCount += 1;
      if (!workspace.connection_error) group.hasReadyWorkspace = true;
      if (group.connectionError === null && workspace.connection_error) group.connectionError = workspace.connection_error;
    }
  }

  const choices: WorkspaceAddGatewayChoice[] = [
    {
      key: "local",
      connectionId: null,
      kind: "local",
      name: t("gateway.workspaceAdd.currentComputer"),
      detail: t("gateway.workspaceAdd.currentComputer"),
      workspaceCount: localWorkspaceCount,
      status: "ready",
      statusLabel: t("gateway.workspaceAdd.available"),
      error: null,
    },
  ];
  for (const [connectionId, group] of remoteGroups) {
    const remote = group.remote;
    if (!remote) {
      throw new Error(t("gateway.workspaceAdd.gatewayConnectionSummaryMissing", connectionId));
    }
    const sshAlias = remote.ssh_config_host?.trim() ?? "";
    const customName = remote.name.trim();
    const displayName =
      sshAlias ||
      (customName && customName !== remote.host ? customName : "") ||
      `${remote.username}@${remote.host}`;
    const address = `${remote.username}@${remote.host}:${remote.port}`;
    choices.push({
      key: connectionId,
      connectionId,
      kind: "remote",
      name: displayName,
      detail: address,
      workspaceCount: group.workspaceCount,
      status: group.hasReadyWorkspace ? "ready" : "offline",
      statusLabel: group.hasReadyWorkspace ? t("gateway.workspaceAdd.sshConnected") : t("gateway.workspaceAdd.sshConnectionError"),
      error: group.connectionError,
    });
  }
  return choices;
}

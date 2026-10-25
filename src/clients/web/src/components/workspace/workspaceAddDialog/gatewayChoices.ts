import type { GatewayWorkspace } from "../../../types/backend";

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
    remote: GatewayWorkspace["remote"];
    workspaceCount: number;
    hasReadyWorkspace: boolean;
    connectionError: string | null;
  }>();
  for (const workspace of workspaces) {
    if (workspace.connection_kind === "local") localWorkspaceCount += 1;
    const connectionId = workspace.remote?.gateway_connection_id;
    if (workspace.connection_kind !== "remote_gateway" || !connectionId) continue;
    let group = remoteGroups.get(connectionId);
    if (!group) {
      group = {
        remote: workspace.remote,
        workspaceCount: 0,
        hasReadyWorkspace: false,
        connectionError: null,
      };
      remoteGroups.set(connectionId, group);
    }
    group.workspaceCount += 1;
    if (!workspace.connection_error) group.hasReadyWorkspace = true;
    if (group.connectionError === null && workspace.connection_error) {
      group.connectionError = workspace.connection_error;
    }
  }

  const choices: WorkspaceAddGatewayChoice[] = [
    {
      key: "local",
      connectionId: null,
      kind: "local",
      name: "当前电脑",
      detail: "当前电脑",
      workspaceCount: localWorkspaceCount,
      status: "ready",
      statusLabel: "可用",
      error: null,
    },
  ];
  for (const [connectionId, group] of remoteGroups) {
    const remote = group.remote;
    if (!remote) {
      throw new Error(`远程 Gateway ${connectionId} 缺少连接摘要`);
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
      statusLabel: group.hasReadyWorkspace ? "SSH 已连接" : "SSH 连接异常",
      error: group.connectionError,
    });
  }
  return choices;
}

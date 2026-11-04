import type { GatewayRemoteConnectionSummary, GatewayWorkspace } from "../../../types/backend";

export interface GatewayWorkspaceGroup {
  key: string;
  title: string;
  connectionLabel: string;
  gatewayLabel: string | null;
  remote: GatewayRemoteConnectionSummary | null;
  workspaces: GatewayWorkspace[];
}

export function groupGatewayWorkspaces(
  workspaces: GatewayWorkspace[],
): GatewayWorkspaceGroup[] {
  const groups = new Map<string, GatewayWorkspaceGroup>();
  for (const workspace of workspaces) {
    if (workspace.connection_kind === "local") {
      const existing = groups.get("local");
      if (existing) {
        existing.workspaces.push(workspace);
      } else {
        groups.set("local", {
          key: "local",
          title: "本机 Gateway",
          connectionLabel: "当前控制面 · 127.0.0.1:8014",
          gatewayLabel: null,
          remote: null,
          workspaces: [workspace],
        });
      }
      continue;
    }
    if (!workspace.remote_gateways.length) {
      throw new Error(
        `远程工作区 ${workspace.workspace_id} 缺少 Gateway 连接摘要`,
      );
    }
    for (const remote of workspace.remote_gateways) {
      const key = `remote:${remote.gateway_connection_id}`;
      const existing = groups.get(key);
      if (existing) {
        existing.workspaces.push(workspace);
        continue;
      }
      groups.set(key, {
        key,
        title: remote.ssh_config_host?.trim() || remote.name.trim() || remote.host,
        connectionLabel:
          `${remote.username}@${remote.host}:${remote.port} → Gateway :${remote.remote_gateway_port}`,
        gatewayLabel: workspace.gateway_id,
        remote,
        workspaces: [workspace],
      });
    }
  }
  return [...groups.values()];
}

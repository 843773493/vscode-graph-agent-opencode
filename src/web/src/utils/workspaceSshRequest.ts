import type {
  AddSshGatewayWorkspaceRequest,
  SshConnectionOption,
} from "../types/backend";

export interface WorkspaceSshFormState {
  name: string;
  remoteGatewayPort: string;
}

export const INITIAL_SSH_WORKSPACE_FORM: WorkspaceSshFormState = {
  name: "",
  remoteGatewayPort: "8014",
};

function required(value: string, label: string): string {
  const trimmed = value.trim();
  if (!trimmed) {
    throw new Error(`${label} 不能为空`);
  }
  return trimmed;
}

export function buildSshWorkspaceRequest(
  form: WorkspaceSshFormState,
  selectedConnection: SshConnectionOption | undefined,
): AddSshGatewayWorkspaceRequest {
  const base = {
    name: form.name.trim() || null,
    remote_gateway_port: Number.parseInt(
      required(form.remoteGatewayPort, "远程 Gateway 端口"),
      10,
    ),
  };
  if (
    !Number.isInteger(base.remote_gateway_port) ||
    base.remote_gateway_port < 1 ||
    base.remote_gateway_port > 65535
  ) {
    throw new Error("远程 Gateway 端口必须是 1-65535");
  }
  if (selectedConnection?.source === "boxteam" && selectedConnection.workspace_id) {
    return {
      ...base,
      connection_workspace_id: selectedConnection.workspace_id,
    };
  }
  if (
    selectedConnection?.source === "ssh_config" &&
    selectedConnection.ssh_config_host
  ) {
    return {
      ...base,
      ssh_config_host: selectedConnection.ssh_config_host,
    };
  }
  throw new Error("请选择已连接的远程 Gateway 或 ~/.ssh/config Host");
}

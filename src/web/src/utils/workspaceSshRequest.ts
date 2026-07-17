import type {
  AddSshGatewayWorkspaceRequest,
  SshConnectionOption,
} from "../types/backend";

export interface WorkspaceSshFormState {
  name: string;
  remoteWorkspacePath: string;
}

export const INITIAL_SSH_WORKSPACE_FORM: WorkspaceSshFormState = {
  name: "",
  remoteWorkspacePath: "",
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
    remote_workspace_path: required(
      form.remoteWorkspacePath,
      "远程工作区路径",
    ),
  };
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
  throw new Error("请选择 BoxTeam SSH 配置或 ~/.ssh/config Host");
}

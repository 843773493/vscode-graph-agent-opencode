import type {
  AddLocalGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  APIResponse,
  GatewayWorkspaceList,
  GatewayHealth,
  GatewayDirectoryList,
  RenameGatewayWorkspaceRequest,
  ReorderGatewayWorkspacesRequest,
  SshConnectionOptionList,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "./types/backend";
import { requestJson, unwrapApiData } from "./api";

export async function getGatewayHealth(port: number): Promise<GatewayHealth> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayHealth>>(
      port,
      "/api/gateway/health",
    ),
  );
}

export async function listGatewayWorkspaces(
  port: number,
): Promise<GatewayWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      "/api/gateway/workspaces",
    ),
  );
}

export async function activateGatewayWorkspace(
  port: number,
  workspaceId: string,
): Promise<string> {
  const result = unwrapApiData(
    await requestJson<APIResponse<{ active_workspace_id: string }>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/activate`,
      { method: "POST" },
    ),
  );
  return result.active_workspace_id;
}

export async function addLocalGatewayWorkspace(
  port: number,
  payload: AddLocalGatewayWorkspaceRequest,
): Promise<GatewayWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      "/api/gateway/workspaces/local",
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function addSshGatewayWorkspace(
  port: number,
  payload: AddSshGatewayWorkspaceRequest,
): Promise<GatewayWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      "/api/gateway/workspaces/ssh",
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function removeGatewayWorkspace(
  port: number,
  workspaceId: string,
): Promise<GatewayWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}`,
      { method: "DELETE" },
    ),
  );
}

export async function renameGatewayWorkspace(
  port: number,
  workspaceId: string,
  payload: RenameGatewayWorkspaceRequest,
): Promise<GatewayWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}`,
      {
        method: "PATCH",
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function reconnectGatewayWorkspace(
  port: number,
  workspaceId: string,
): Promise<GatewayWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/reconnect`,
      { method: "POST" },
    ),
  );
}

export async function reorderGatewayWorkspaces(
  port: number,
  payload: ReorderGatewayWorkspacesRequest,
): Promise<GatewayWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      "/api/gateway/workspaces/order",
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function getGatewayUiSettings(port: number): Promise<WebUiSettings> {
  return unwrapApiData(
    await requestJson<APIResponse<WebUiSettings>>(
      port,
      "/api/gateway/ui-settings",
    ),
  );
}

export async function updateGatewayUiSettings(
  port: number,
  payload: WebUiSettingsUpdate,
): Promise<WebUiSettings> {
  return unwrapApiData(
    await requestJson<APIResponse<WebUiSettings>>(
      port,
      "/api/gateway/ui-settings",
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function browseGatewayLocalDirectories(
  port: number,
  path?: string | null,
): Promise<GatewayDirectoryList> {
  const query = new URLSearchParams();
  if (path?.trim()) {
    query.set("path", path.trim());
  }
  const suffix = query.toString();
  return unwrapApiData(
    await requestJson<APIResponse<GatewayDirectoryList>>(
      port,
      `/api/gateway/local-directories${suffix ? `?${suffix}` : ""}`,
    ),
  );
}

export async function listGatewaySshConnections(
  port: number,
): Promise<SshConnectionOptionList> {
  return unwrapApiData(
    await requestJson<APIResponse<SshConnectionOptionList>>(
      port,
      "/api/gateway/ssh-connections",
    ),
  );
}

export async function browseGatewayRemoteDirectories(
  port: number,
  connectionId: string,
  path?: string | null,
): Promise<GatewayDirectoryList> {
  const query = new URLSearchParams();
  if (path?.trim()) {
    query.set("path", path.trim());
  }
  const suffix = query.toString();
  return unwrapApiData(
    await requestJson<APIResponse<GatewayDirectoryList>>(
      port,
      `/api/gateway/ssh-connections/${encodeURIComponent(connectionId)}/directories${suffix ? `?${suffix}` : ""}`,
    ),
  );
}

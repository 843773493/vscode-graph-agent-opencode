import type {
  AddLocalGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  APIResponse,
  GatewayWorkspaceList,
} from "./types/backend";
import { requestJson, unwrapApiData } from "./api";

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

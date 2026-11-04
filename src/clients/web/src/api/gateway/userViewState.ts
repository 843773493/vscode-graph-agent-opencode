import { workspaceOwnerFromKey } from "../workspaceIdentity";
import type {
  APIResponse,
  GatewayUserViewState,
  GatewayUserViewStateUpdateRequest,
} from "../../types/backend";
import { requestJson, unwrapApiData, unwrapApiDataOrNull } from "../http";

function userViewStatePath(workspaceKey: string, sessionId: string): string {
  const owner = workspaceOwnerFromKey(workspaceKey);
  const query = new URLSearchParams({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, session_id: sessionId });
  return `/api/gateway/users/current/view-state?${query}`;
}

export async function getGatewayUserViewState(
  port: number,
  workspaceId: string,
  sessionId: string,
): Promise<GatewayUserViewState | null> {
  return unwrapApiDataOrNull(
    await requestJson<APIResponse<GatewayUserViewState | null>>(
      port,
      userViewStatePath(workspaceId, sessionId),
    ),
  );
}

export async function getLatestGatewayUserViewState(
  port: number,
): Promise<GatewayUserViewState | null> {
  return unwrapApiDataOrNull(
    await requestJson<APIResponse<GatewayUserViewState | null>>(
      port,
      "/api/gateway/users/current/view-state/latest",
    ),
  );
}

export async function putGatewayUserViewState(
  port: number,
  workspaceId: string,
  sessionId: string,
  payload: GatewayUserViewStateUpdateRequest,
): Promise<GatewayUserViewState> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUserViewState>>(
      port,
      userViewStatePath(workspaceId, sessionId),
      { method: "PUT", body: JSON.stringify(payload) },
    ),
  );
}

import type {
  APIResponse,
  GatewayUserViewState,
  GatewayUserViewStateUpdateRequest,
} from "../../types/backend";
import { requestJson, unwrapApiData } from "../http";

function userViewStatePath(workspaceId: string, sessionId: string): string {
  return `/api/gateway/users/current/view-state?workspace_id=${encodeURIComponent(workspaceId)}&session_id=${encodeURIComponent(sessionId)}`;
}

export async function getGatewayUserViewState(
  port: number,
  workspaceId: string,
  sessionId: string,
): Promise<GatewayUserViewState | null> {
  const response = await requestJson<APIResponse<GatewayUserViewState | null>>(
    port,
    userViewStatePath(workspaceId, sessionId),
  );
  if (!response.request_id) throw new Error("用户视图状态响应缺少 request_id");
  return response.data;
}

export async function getLatestGatewayUserViewState(
  port: number,
): Promise<GatewayUserViewState | null> {
  const response = await requestJson<APIResponse<GatewayUserViewState | null>>(
    port,
    "/api/gateway/users/current/view-state/latest",
  );
  if (!response.request_id) throw new Error("用户最新视图状态响应缺少 request_id");
  return response.data;
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

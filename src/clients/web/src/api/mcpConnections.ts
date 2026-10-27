import type { APIResponse } from "../types/backend";
import type { McpAuthorizationCallbackDTO, McpAuthorizationDTO, McpConnectionOperationDTO, McpServerDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import type { GatewayWorkspaceRequestTarget } from "./workspaceIdentity";
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData } from "./http";

export type { McpConnectionOperationDTO, McpServerDTO };

async function request<T>(port: number, target: GatewayWorkspaceRequestTarget, path: string, init: RequestInit = {}): Promise<T> {
  return unwrapApiData(await requestJson<APIResponse<T>>(port, `/api/v1/mcp/${path}`, {
    ...init, headers: gatewayWorkspaceRequestHeaders(target), cache: "no-store",
  }));
}

export function listMcpServers(port: number, target: GatewayWorkspaceRequestTarget, signal?: AbortSignal) {
  return request<McpServerDTO[]>(port, target, "servers", { signal });
}

export function listMcpOperations(port: number, target: GatewayWorkspaceRequestTarget, signal?: AbortSignal) {
  return request<McpConnectionOperationDTO[]>(port, target, "connection-operations", { signal });
}

export function submitMcpOperation(port: number, target: GatewayWorkspaceRequestTarget, serverId: string, action: "connect" | "disconnect", signal: AbortSignal) {
  return request<McpConnectionOperationDTO>(port, target, "connection-operations", {
    method: "POST", body: JSON.stringify({ operation_id: crypto.randomUUID(), server_id: serverId, action }), signal,
  });
}

export function cancelMcpOperation(port: number, target: GatewayWorkspaceRequestTarget, operationId: string, signal: AbortSignal) {
  return request<McpConnectionOperationDTO>(port, target, `connection-operations/${encodeURIComponent(operationId)}/cancel`, { method: "POST", signal });
}

export function getMcpAuthorization(port: number, target: GatewayWorkspaceRequestTarget, operationId: string, signal: AbortSignal) {
  return request<McpAuthorizationDTO>(port, target, `connection-operations/${encodeURIComponent(operationId)}/authorization`, { signal });
}

export function completeMcpAuthorization(port: number, target: GatewayWorkspaceRequestTarget, operationId: string, callback: McpAuthorizationCallbackDTO, signal: AbortSignal) {
  return request<McpConnectionOperationDTO>(port, target, `connection-operations/${encodeURIComponent(operationId)}/callback`, {
    method: "POST", body: JSON.stringify(callback), signal,
  });
}

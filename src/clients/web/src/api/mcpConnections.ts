import type {
  McpAuthorizationCallbackDTO, McpAuthorizationResponseDTO, McpConnectionOperationDTO,
  McpConnectionOperationListResponseDTO, McpConnectionOperationResponseDTO, McpServerDTO, McpServerListResponseDTO,
} from "../types/protocol_generated/boxteam/workspace/v2/public";
import type { GatewayWorkspaceRequestTarget } from "./workspaceIdentity";
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData } from "./http";

export type { McpConnectionOperationDTO, McpServerDTO };

type McpResponse = McpServerListResponseDTO | McpConnectionOperationListResponseDTO | McpConnectionOperationResponseDTO | McpAuthorizationResponseDTO;

async function request<T extends McpResponse>(port: number, target: GatewayWorkspaceRequestTarget, path: string, init: RequestInit = {}): Promise<T> {
  const { gateway_id, workspace_id } = target;
  const response = await requestJson<T>(port, `/api/v1/mcp/${path}`, {
    ...init, headers: gatewayWorkspaceRequestHeaders(target), cache: "no-store",
  });
  if (response.protocol_version !== 1 || response.target?.kind !== "workspace") {
    throw new Error("MCP 管理响应的协议版本或目标类型不受支持");
  }
  if (response.target.gateway_id !== gateway_id || response.target.workspace_id !== workspace_id) {
    throw new Error("MCP 管理响应不属于请求的 Gateway／工作区");
  }
  return response;
}

export async function getMcpCatalog(port: number, target: GatewayWorkspaceRequestTarget, signal?: AbortSignal) {
  const response = await request<McpServerListResponseDTO>(port, target, "servers", { signal });
  if (!/^sha256:[0-9a-f]{64}$/.test(response.catalog_revision) || !Number.isSafeInteger(response.catalog_generation) || response.catalog_generation < 1) {
    throw new Error("MCP 管理响应缺少有效目录 revision／generation");
  }
  unwrapApiData(response);
  return response;
}

export async function listMcpOperations(port: number, target: GatewayWorkspaceRequestTarget, signal?: AbortSignal) {
  return unwrapApiData(await request<McpConnectionOperationListResponseDTO>(port, target, "connection-operations", { signal }));
}

export async function submitMcpOperation(port: number, target: GatewayWorkspaceRequestTarget, serverId: string, action: "connect" | "disconnect", signal: AbortSignal) {
  const response = await request<McpConnectionOperationResponseDTO>(port, target, "connection-operations", {
    method: "POST", body: JSON.stringify({ operation_id: crypto.randomUUID(), server_id: serverId, action }), signal,
  });
  return unwrapApiData({ ...response, data: response.data ?? null });
}

export async function cancelMcpOperation(port: number, target: GatewayWorkspaceRequestTarget, operationId: string, signal: AbortSignal) {
  const response = await request<McpConnectionOperationResponseDTO>(port, target, `connection-operations/${encodeURIComponent(operationId)}/cancel`, { method: "POST", signal });
  return unwrapApiData({ ...response, data: response.data ?? null });
}

export async function getMcpAuthorization(port: number, target: GatewayWorkspaceRequestTarget, operationId: string, signal: AbortSignal) {
  const response = await request<McpAuthorizationResponseDTO>(port, target, `connection-operations/${encodeURIComponent(operationId)}/authorization`, { signal });
  return unwrapApiData({ ...response, data: response.data ?? null });
}

export async function completeMcpAuthorization(port: number, target: GatewayWorkspaceRequestTarget, operationId: string, callback: McpAuthorizationCallbackDTO, signal: AbortSignal) {
  const response = await request<McpConnectionOperationResponseDTO>(port, target, `connection-operations/${encodeURIComponent(operationId)}/callback`, {
    method: "POST", body: JSON.stringify(callback), signal,
  });
  return unwrapApiData({ ...response, data: response.data ?? null });
}

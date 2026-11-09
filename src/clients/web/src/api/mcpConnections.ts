import type {
  McpAuthorizationCallbackDTO, McpAuthorizationResponseDTO, McpConnectionOperationDTO,
  McpConnectionOperationListResponseDTO, McpConnectionOperationResponseDTO, McpServerDTO, McpServerListResponseDTO,
  McpPrivateHeaderRefDTO, McpPrivateHeaderWriteRequestDTO, McpPrivateHeaderWriteResponseDTO,
} from "../types/protocol_generated/boxteam/workspace/v2/public";
import type { GatewayWorkspaceRequestTarget } from "./workspaceIdentity";
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData } from "./http";

export type { McpConnectionOperationDTO, McpServerDTO };

type McpResponse = McpServerListResponseDTO | McpConnectionOperationListResponseDTO | McpConnectionOperationResponseDTO | McpAuthorizationResponseDTO
  | McpPrivateHeaderWriteResponseDTO;

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

export async function getMcpCatalog(port: number, target: GatewayWorkspaceRequestTarget, { refresh = true, signal }: { refresh?: boolean; signal?: AbortSignal } = {}) {
  const response = await request<McpServerListResponseDTO>(port, target, `servers?refresh=${refresh}`, { signal });
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

export async function writeMcpPrivateHeader(port: number, target: GatewayWorkspaceRequestTarget, payload: McpPrivateHeaderWriteRequestDTO, signal?: AbortSignal): Promise<McpPrivateHeaderRefDTO> {
  const response = await request<McpPrivateHeaderWriteResponseDTO>(port, target, "private-headers", {
    method: "POST", body: JSON.stringify(payload), signal,
  });
  const reference = unwrapApiData({ ...response, data: response.data ?? null });
  if (reference.kind !== "mcp_private_header" || reference.workspace_id !== target.workspace_id
    || reference.server_id !== payload.server_id || reference.header_name.toLowerCase() !== payload.header_name.toLowerCase()
    || reference.endpoint !== payload.endpoint) {
    throw new Error("MCP 私有 Header 写入响应不属于本次目标或请求");
  }
  return reference;
}

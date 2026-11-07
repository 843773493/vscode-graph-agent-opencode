import type {
  McpAuthorizationCallbackDTO, McpAuthorizationResponseDTO, McpConnectionOperationDTO,
  McpConnectionOperationListResponseDTO, McpConnectionOperationResponseDTO, McpServerDTO, McpServerListResponseDTO,
  McpCandidateInspectRequestDTO, McpCandidateInspectResponseDTO, McpCandidatePlanRequestDTO, McpCandidatePlanResponseDTO,
  McpInstallationCapabilitiesResponseDTO, McpInstallationTargetDTO, McpInstallationRequestDTO,
  McpInstallationOperationResponseDTO, McpInstalledArtifactListResponseDTO,
  McpPrivateHeaderRefDTO, McpPrivateHeaderWriteRequestDTO, McpPrivateHeaderWriteResponseDTO,
  McpPrivateEnvRefDTO, McpPrivateEnvWriteRequestDTO, McpPrivateEnvWriteResponseDTO,
  McpCandidateConfigurationRequestDTO, McpCandidateConfigurationResponseDTO,
  McpCandidateConfigurationDraftRequestDTO, McpCandidateConfigurationDraftResponseDTO,
  McpArtifactReferencesResponseDTO, McpManagedLaunchDTO, McpUninstallationRequestDTO,
  McpInstallationTargetListRequestDTO, McpInstallationTargetListResponseDTO,
} from "../types/protocol_generated/boxteam/workspace/v2/public";
import type { GatewayWorkspaceRequestTarget } from "./workspaceIdentity";
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData } from "./http";

export type { McpConnectionOperationDTO, McpServerDTO };

export type McpInstallationRequestTarget = McpInstallationTargetDTO & { gateway_connection_id?: string };

type InstallationResponse = McpCandidateInspectResponseDTO | McpCandidatePlanResponseDTO | McpInstallationCapabilitiesResponseDTO
  | McpInstallationOperationResponseDTO | McpInstalledArtifactListResponseDTO | McpArtifactReferencesResponseDTO;

function installationResponse<T extends InstallationResponse>(response: T, expected: McpInstallationTargetDTO): T {
  if (response.protocol_version !== 1 || response.target?.kind !== "backend"
      || !/^[0-9a-f-]{36}$/.test(response.target.backend_id)) {
    throw new Error("MCP 制品响应缺少精确原生 Gateway／Backend 或受支持协议");
  }
  if (response.target.gateway_id !== expected.gateway_id || response.target.backend_id !== expected.backend_id) {
    throw new Error("MCP 制品响应的 Backend 已改变，不能改投当前目标");
  }
  unwrapApiData<InstallationResponse["data"]>(response);
  return response;
}

async function installationRequest<T extends InstallationResponse>(port: number, target: McpInstallationRequestTarget, path: string,
  init: RequestInit = {}, payloadTarget?: McpInstallationTargetDTO): Promise<T> {
  const expected = { kind: target.kind, gateway_id: target.gateway_id, backend_id: target.backend_id };
  if (expected.kind !== "backend" || !expected.gateway_id || !expected.backend_id
      || payloadTarget && (payloadTarget.kind !== "backend" || payloadTarget.gateway_id !== expected.gateway_id || payloadTarget.backend_id !== expected.backend_id)) {
    throw new Error("MCP 制品请求必须提交给原 Gateway／Backend 安装域");
  }
  const connection = target.gateway_connection_id ? `?gateway_connection_id=${encodeURIComponent(target.gateway_connection_id)}` : "";
  const response = await requestJson<T>(port,
    `/api/gateway/mcp/${encodeURIComponent(expected.gateway_id)}/${encodeURIComponent(expected.backend_id)}/${path}${connection}`,
    { ...init, cache: "no-store" });
  return installationResponse(response, expected);
}

export async function listMcpInstallationTargets(port: number, target: Pick<McpInstallationRequestTarget, "gateway_id" | "gateway_connection_id">, signal?: AbortSignal) {
  const gatewayId = target.gateway_id;
  const payload: McpInstallationTargetListRequestDTO = { protocol_version: 1, gateway_id: gatewayId, gateway_connection_id: target.gateway_connection_id };
  const response = await requestJson<McpInstallationTargetListResponseDTO>(port, "/api/gateway/mcp-installation-targets",
    { method: "POST", body: JSON.stringify(payload), signal, cache: "no-store" });
  if (response.code !== 0 || typeof response.message !== 'string') throw new Error('MCP 安装域发现缺少有效成功回执');
  const targets = unwrapApiData({ ...response, code: response.code, message: response.message });
  if (response.protocol_version !== 1 || response.gateway_id !== gatewayId || response.source !== "live"
      || targets.some(item => item.kind !== "backend" || item.gateway_id !== gatewayId || !/^[0-9a-f-]{36}$/.test(item.backend_id))) {
    throw new Error("MCP 安装域发现返回了不同目标或非实时目录");
  }
  return targets;
}

export function getMcpInstallationCapabilities(port: number, target: McpInstallationRequestTarget, signal?: AbortSignal) {
  return installationRequest<McpInstallationCapabilitiesResponseDTO>(port, target, "installation-capabilities", { signal });
}

export function inspectMcpInstallationCandidate(port: number, target: McpInstallationRequestTarget, candidate: McpCandidateInspectRequestDTO, signal?: AbortSignal) {
  return installationRequest<McpCandidateInspectResponseDTO>(port, target, "candidate-manifests",
    { method: "POST", body: JSON.stringify(candidate), signal }, candidate.target);
}

export function planMcpInstallation(port: number, target: McpInstallationRequestTarget, candidate: McpCandidatePlanRequestDTO, signal?: AbortSignal) {
  return installationRequest<McpCandidatePlanResponseDTO>(port, target, "candidate-plans",
    { method: "POST", body: JSON.stringify(candidate), signal }, candidate.target);
}

export function installMcpArtifact(port: number, target: McpInstallationRequestTarget, request: McpInstallationRequestDTO, signal?: AbortSignal) {
  return installationRequest<McpInstallationOperationResponseDTO>(port, target, "installations",
    { method: "POST", body: JSON.stringify(request), signal }, request.candidate!.target);
}

export function getMcpInstallationOperation(port: number, target: McpInstallationRequestTarget, operationId: string, signal?: AbortSignal) {
  return installationRequest<McpInstallationOperationResponseDTO>(port, target, `installation-operations/${encodeURIComponent(operationId)}`, { signal });
}

export function cancelMcpInstallationOperation(port: number, target: McpInstallationRequestTarget, operationId: string, signal?: AbortSignal) {
  const payload = { kind: target.kind, gateway_id: target.gateway_id, backend_id: target.backend_id };
  return installationRequest<McpInstallationOperationResponseDTO>(port, target, `installation-operations/${encodeURIComponent(operationId)}/cancel`,
    { method: "POST", body: JSON.stringify(payload), signal });
}

export function listMcpInstalledArtifacts(port: number, target: McpInstallationRequestTarget, signal?: AbortSignal) {
  return installationRequest<McpInstalledArtifactListResponseDTO>(port, target, "installations", { signal });
}

export async function getMcpArtifactReferences(port: number, target: McpInstallationRequestTarget, binding: McpManagedLaunchDTO, signal?: AbortSignal) {
  const expected = { ...binding };
  const response = await installationRequest<McpArtifactReferencesResponseDTO>(port, target,
    `installations/${encodeURIComponent(expected.installation_id)}/references`, { signal }, target);
  const actual = response.data?.binding;
  if (!actual || actual.backend_id !== expected.backend_id || actual.installation_id !== expected.installation_id
      || actual.generation !== expected.generation || actual.artifact_digest !== expected.artifact_digest) {
    throw new Error("MCP 制品引用不属于原 generation，不能用于卸载决定");
  }
  return response;
}

export function uninstallMcpArtifact(port: number, target: McpInstallationRequestTarget, request: McpUninstallationRequestDTO, signal?: AbortSignal) {
  return installationRequest<McpInstallationOperationResponseDTO>(port, target,
    `installations/${encodeURIComponent(request.binding!.installation_id)}`,
    { method: "DELETE", body: JSON.stringify(request), signal }, request.target);
}

type McpResponse = McpServerListResponseDTO | McpConnectionOperationListResponseDTO | McpConnectionOperationResponseDTO | McpAuthorizationResponseDTO
  | McpPrivateHeaderWriteResponseDTO | McpPrivateEnvWriteResponseDTO | McpCandidateConfigurationResponseDTO
  | McpCandidateConfigurationDraftResponseDTO;

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

export async function planMcpCandidateConfiguration(port: number, workspace: GatewayWorkspaceRequestTarget, candidate: McpCandidatePlanRequestDTO, signal?: AbortSignal) {
  const expected = { ...candidate.target! };
  if (expected.kind !== "backend" || expected.gateway_id !== workspace.gateway_id) throw new Error("MCP 配置计划必须属于原 Gateway／Backend");
  const response = await requestJson<McpCandidatePlanResponseDTO>(port, "/api/v1/mcp/candidate-configurations/plans", {
    method: "POST", body: JSON.stringify(candidate), headers: gatewayWorkspaceRequestHeaders(workspace), signal, cache: "no-store",
  });
  return installationResponse(response, expected);
}

export async function writeMcpPrivateHeader(
  port: number, target: GatewayWorkspaceRequestTarget, payload: McpPrivateHeaderWriteRequestDTO, signal?: AbortSignal,
) {
  const { gateway_id, workspace_id } = target;
  const { server_id, header_name, endpoint } = payload;
  if (payload.target?.kind !== "workspace" || payload.target.gateway_id !== gateway_id
      || payload.target.workspace_id !== workspace_id) {
    throw new Error("MCP 私有 header 必须提交给原 Gateway／工作区");
  }
  const response = await request<McpPrivateHeaderWriteResponseDTO>(port, target, "private-headers", {
    method: "POST", body: JSON.stringify(payload), signal,
  });
  const reference = unwrapApiData<McpPrivateHeaderRefDTO>({ ...response, data: response.data ?? null });
  if (reference.kind !== "mcp_private_header" || reference.workspace_id !== workspace_id
      || reference.server_id !== server_id || reference.header_name !== header_name
      || reference.endpoint !== endpoint || !/^[0-9a-f]{32}$/.test(reference.version)) {
    throw new Error("MCP 私有 header 回执不属于原输入身份或缺少精确版本");
  }
  return reference;
}

export async function writeMcpPrivateEnv(
  port: number, target: GatewayWorkspaceRequestTarget, payload: McpPrivateEnvWriteRequestDTO, signal?: AbortSignal,
) {
  const { gateway_id, workspace_id } = target;
  const { server_id, input_key } = payload;
  if (payload.target?.gateway_id !== gateway_id || payload.target.workspace_id !== workspace_id) {
    throw new Error("MCP 私有 env 必须提交给原 Gateway／工作区");
  }
  const response = await request<McpPrivateEnvWriteResponseDTO>(port, target,
    payload.candidate ? "candidate-plans/private-env" : "private-env", { method: "POST", body: JSON.stringify(payload), signal });
  const reference = unwrapApiData<McpPrivateEnvRefDTO>({ ...response, data: response.data ?? null });
  if (reference.kind !== "mcp_private_env" || reference.workspace_id !== workspace_id
      || reference.server_id !== server_id || reference.input_key !== input_key
      || !/^sha256:jcs:v1:[0-9a-f]{64}$/.test(reference.definition_digest) || !/^[0-9a-f]{32}$/.test(reference.version)) {
    throw new Error("MCP 私有 env 回执不属于原输入身份或缺少精确版本");
  }
  return reference;
}

export async function draftMcpCandidateConfiguration(
  port: number, target: GatewayWorkspaceRequestTarget, payload: McpCandidateConfigurationDraftRequestDTO, signal?: AbortSignal,
) {
  const { gateway_id, workspace_id } = target;
  const { source_key, idempotency_key } = payload.source_edit!;
  if (payload.target?.gateway_id !== gateway_id || payload.target.workspace_id !== workspace_id) {
    throw new Error("MCP 配置提案必须提交给原 Gateway／工作区");
  }
  const response = await request<McpCandidateConfigurationDraftResponseDTO>(port, target, "candidate-configurations/draft",
    { method: "POST", body: JSON.stringify(payload), signal });
  const draft = unwrapApiData({ ...response, data: response.data ?? null });
  const edit = draft.source_edit;
  const document = draft.preflight?.document;
  if (edit?.gateway_id !== gateway_id || edit.workspace_id !== workspace_id
      || edit.source_key !== source_key || edit.idempotency_key !== idempotency_key
      || document?.gateway_id !== gateway_id || document.workspace_id !== workspace_id
      || document.source_key !== source_key) {
    throw new Error("MCP 配置提案不属于原目标或来源提交");
  }
  return draft;
}

export async function saveMcpCandidateConfiguration(
  port: number, target: GatewayWorkspaceRequestTarget, payload: McpCandidateConfigurationRequestDTO, signal?: AbortSignal,
) {
  const { gateway_id, workspace_id } = target;
  const sourceKey = payload.source_edit?.source_key;
  const idempotencyKey = payload.source_edit?.idempotency_key;
  if (payload.target?.gateway_id !== gateway_id || payload.target.workspace_id !== workspace_id) {
    throw new Error("MCP 候选配置必须提交给原 Gateway／工作区");
  }
  const response = await request<McpCandidateConfigurationResponseDTO>(port, target, "candidate-configurations",
    { method: "PUT", body: JSON.stringify(payload), signal });
  const document = unwrapApiData({ ...response, data: response.data ?? null });
  if (document.gateway_id !== gateway_id || document.workspace_id !== workspace_id
      || document.source_key !== sourceKey || document.edit?.idempotency_key !== idempotencyKey) {
    throw new Error("MCP 候选配置回执不属于原来源提交");
  }
  return document;
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

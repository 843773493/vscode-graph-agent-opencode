import { gatewayWorkspaceKey, workspaceOwnerFromKey, type GatewayRequestTarget } from "./api/workspaceIdentity";
import { formatNumber, t } from "../../shared/core/i18n/index.js";
import type { GatewayDiagnosticLogLocateRequest, GatewayDiagnosticLogLocationDTO, GatewayDiagnosticLogQueryRequest, GatewayDiagnosticLogQueryResultDTO, GatewayManagementDirectoryDTO } from "./types/protocol_generated/boxteam/gateway/v2/public";
import type {
  AddManagedGatewayWorkspaceRequest,
  AddRemoteGatewayConnectionRequest,
  ActivateGatewayWorkspaceResultDTO,
  APIResponse,
  GatewayInboundAccessList,
  GatewayDeviceConnectionList,
  GatewayDeviceAccessAddressList,
  CreatedGatewayDeviceConnection,
  GatewayWorkspaceList,
  GatewayRuntimeRestartResult,
  GatewayRuntimeGroupRequest,
  GatewayRuntimeStateResult,
  GatewayHealth,
  GatewayDiagnostics,
  DevelopmentRuntimeRestartResult,
  GatewayDirectoryList,
  GatewayManagedWorkspaceList,
  UpdateGatewayWorkspaceRequest,
  ReorderGatewayWorkspacesRequest,
  SshConnectionOptionList,
  GatewayUiAsset,
  GatewayUiAssetList,
  WorkspaceNavigationTree,
  CreateGatewayPortForwardRequest,
  GatewayPortForwardList,
  GatewayResourceList,
  WorkspaceFolderCreateRequest,
  WorkspaceNavigationNodeUpdateRequest,
  WorkspaceNavigationPlacementRequest,
} from "./types/backend";
import {
  DEFAULT_API_REQUEST_TIMEOUT_MS,
  HttpRequestError,
  LIFECYCLE_REQUEST_TIMEOUT_MS,
  normalizePageResult,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "./api/http";

function gatewayWorkspacePath(workspaceKey: string): string {
  return `/api/gateway/workspaces/${encodeURIComponent(workspaceOwnerFromKey(workspaceKey).workspaceId)}`;
}

export async function getGatewayHealth(port: number, options: { signal?: AbortSignal } = {}): Promise<GatewayHealth> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayHealth>>(
      port,
      "/api/gateway/health",
      options,
    ),
  );
}

export async function listGatewayManagementTargets(port: number, signal?: AbortSignal): Promise<GatewayManagementDirectoryDTO> {
  return unwrapApiData(await requestJson<APIResponse<GatewayManagementDirectoryDTO>>(
    port, "/api/gateway/management/gateways", { signal },
  ));
}

export type GatewayLogTarget = GatewayRequestTarget & {
  log_id?: string;
  request_id?: string;
  location?: GatewayDiagnosticLogLocateRequest;
};

export async function getGatewayDiagnostics(
  port: number,
  options: {
    target?: GatewayRequestTarget;
    logId?: string | null;
    tailLines?: number;
    includeTail?: boolean;
    signal?: AbortSignal;
  } = {},
): Promise<GatewayDiagnostics> {
  const params = new URLSearchParams();
  const target = options.target;
  if (target?.gateway_connection_id) {
    params.set("gateway_connection_id", target.gateway_connection_id);
  }
  if (target?.workspace_id) params.set("workspace_id", target.workspace_id);
  if (options.logId) params.set("log_id", options.logId);
  if (options.includeTail !== undefined) params.set("include_tail", String(options.includeTail));
  if (options.tailLines) params.set("tail_lines", String(options.tailLines));
  const query = params.toString();
  return unwrapApiData(
    await requestJson<APIResponse<GatewayDiagnostics>>(
      port,
      `/api/gateway/diagnostics${query ? `?${query}` : ""}`,
      { headers: target ? { "X-BoxTeam-Target-Gateway-Id": target.gateway_id,
          ...(target.workspace_id ? { "X-BoxTeam-Workspace-Id": target.workspace_id } : {}) } : {},
        timeoutMs: DEFAULT_API_REQUEST_TIMEOUT_MS, signal: options.signal },
    ),
  );
}

export async function queryGatewayDiagnosticLogs(
  port: number, target: GatewayRequestTarget, request: GatewayDiagnosticLogQueryRequest, signal?: AbortSignal,
): Promise<GatewayDiagnosticLogQueryResultDTO> {
  const query = new URLSearchParams();
  if (target.gateway_connection_id) query.set("gateway_connection_id", target.gateway_connection_id);
  const suffix = query.size ? `?${query}` : "";
  return unwrapApiData(await requestJson<APIResponse<GatewayDiagnosticLogQueryResultDTO>>(
    port, `/api/gateway/diagnostics/query${suffix}`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-BoxTeam-Target-Gateway-Id": target.gateway_id },
      body: JSON.stringify(request), signal,
    },
  ));
}

export async function locateGatewayDiagnosticLog(
  port: number, reference: GatewayDiagnosticLogLocateRequest, options: { gatewayConnectionId?: string; signal?: AbortSignal } = {},
): Promise<GatewayDiagnosticLogLocationDTO> {
  const query = new URLSearchParams();
  if (options.gatewayConnectionId) query.set("gateway_connection_id", options.gatewayConnectionId);
  const suffix = query.size ? `?${query}` : "";
  return unwrapApiData(await requestJson<APIResponse<GatewayDiagnosticLogLocationDTO>>(
    port, `/api/gateway/diagnostics/locate${suffix}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(reference), signal: options.signal,
    },
  ));
}

export async function restartDevelopmentRuntime(
  port: number,
): Promise<DevelopmentRuntimeRestartResult> {
  return unwrapApiData(
    await requestJson<APIResponse<DevelopmentRuntimeRestartResult>>(
      port,
      "/api/gateway/runtime/restart-development",
      { method: "POST", timeoutMs: LIFECYCLE_REQUEST_TIMEOUT_MS },
    ),
  );
}

export async function listGatewayWorkspaces(
  port: number,
  options: { checkHealth?: boolean } = {},
): Promise<GatewayWorkspaceList> {
  const query = options.checkHealth === undefined
    ? ""
    : `?check_health=${String(options.checkHealth)}`;
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      `/api/gateway/workspaces${query}`,
    ),
  );
}

export async function listGatewayResources(
  port: number,
): Promise<GatewayResourceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayResourceList>>(
      port,
      "/api/gateway/resources",
    ),
  );
}

export async function listPortForwards(port: number): Promise<GatewayPortForwardList> {
  return unwrapApiData(await requestJson<APIResponse<GatewayPortForwardList>>(port, "/api/gateway/port-forwards"));
}

export async function listPortForwardHosts(port: number) {
  return unwrapApiData(await requestJson<APIResponse<import("./types/gatewayProtocol").PortForwardHostsDTO>>(port, "/api/gateway/port-forwards/hosts"));
}

export async function savePortForward(port: number, payload: CreateGatewayPortForwardRequest, forwardId?: string): Promise<GatewayPortForwardList> {
  return unwrapApiData(await requestJson<APIResponse<GatewayPortForwardList>>(port,
    `/api/gateway/port-forwards${forwardId ? `/${encodeURIComponent(forwardId)}` : ''}`,
    { method: forwardId ? "PUT" : "POST", body: JSON.stringify(payload) }));
}

export async function setPortForwardRunning(port: number, forwardId: string, enabled: boolean): Promise<GatewayPortForwardList> {
  return unwrapApiData(await requestJson<APIResponse<GatewayPortForwardList>>(port,
    `/api/gateway/port-forwards/${encodeURIComponent(forwardId)}/${enabled ? 'start' : 'stop'}`,
    { method: "POST" }));
}

export async function deletePortForward(port: number, forwardId: string): Promise<GatewayPortForwardList> {
  return unwrapApiData(await requestJson<APIResponse<GatewayPortForwardList>>(port,
    `/api/gateway/port-forwards/${encodeURIComponent(forwardId)}`, { method: "DELETE" }));
}

export async function getWorkspaceNavigation(
  port: number,
): Promise<WorkspaceNavigationTree> {
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceNavigationTree>>(
      port,
      "/api/gateway/workspace-navigation",
    ),
  );
}

export async function createWorkspaceNavigationFolder(
  port: number,
  name: string,
  parentNodeId?: string | null,
): Promise<WorkspaceNavigationTree> {
  const payload: WorkspaceFolderCreateRequest = {
    name,
    parent_node_id: parentNodeId ?? null,
  };
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceNavigationTree>>(
      port,
      "/api/gateway/workspace-navigation/folders",
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function renameWorkspaceNavigationFolder(
  port: number,
  nodeId: string,
  name: string,
): Promise<WorkspaceNavigationTree> {
  const payload: WorkspaceNavigationNodeUpdateRequest = { name };
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceNavigationTree>>(
      port,
      `/api/gateway/workspace-navigation/nodes/${encodeURIComponent(nodeId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  );
}

export async function deleteWorkspaceNavigationFolder(
  port: number,
  nodeId: string,
): Promise<WorkspaceNavigationTree> {
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceNavigationTree>>(
      port,
      `/api/gateway/workspace-navigation/folders/${encodeURIComponent(nodeId)}?recursive=true`,
      { method: "DELETE" },
    ),
  );
}

export async function placeWorkspaceNavigationNode(
  port: number,
  payload: WorkspaceNavigationPlacementRequest,
): Promise<WorkspaceNavigationTree> {
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceNavigationTree>>(
      port,
      "/api/gateway/workspace-navigation/placement",
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function listGatewayInboundAccess(
  port: number,
): Promise<GatewayInboundAccessList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayInboundAccessList>>(
      port,
      "/api/gateway/inbound-access",
    ),
  );
}

export async function listGatewayDeviceConnections(
  port: number,
): Promise<GatewayDeviceConnectionList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayDeviceConnectionList>>(
      port,
      "/api/gateway/device-connections",
    ),
  );
}

export async function listGatewayDeviceAccessAddresses(
  port: number,
): Promise<GatewayDeviceAccessAddressList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayDeviceAccessAddressList>>(
      port,
      "/api/gateway/device-connections/access-addresses",
    ),
  );
}

export async function createGatewayDeviceConnection(
  port: number,
  payload: { device_name: string; gateway_url: string },
): Promise<CreatedGatewayDeviceConnection> {
  return unwrapApiData(
    await requestJson<APIResponse<CreatedGatewayDeviceConnection>>(
      port,
      "/api/gateway/device-connections",
      { method: "POST", body: JSON.stringify(payload) },
    ),
  );
}

export async function revokeGatewayDeviceConnection(
  port: number,
  deviceConnectionId: string,
): Promise<GatewayDeviceConnectionList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayDeviceConnectionList>>(
      port,
      `/api/gateway/device-connections/${encodeURIComponent(deviceConnectionId)}`,
      { method: "DELETE" },
    ),
  );
}

export async function activateGatewayWorkspace(
  port: number,
  workspaceId: string,
  signal?: AbortSignal,
): Promise<string> {
  const result = unwrapApiData(
    await requestJson<APIResponse<ActivateGatewayWorkspaceResultDTO>>(
      port,
      `${gatewayWorkspacePath(workspaceId)}/activate`,
      { headers: workspaceHeader(workspaceId), method: "POST", signal, timeoutMs: DEFAULT_API_REQUEST_TIMEOUT_MS },
    ),
  );
  return gatewayWorkspaceKey(result.active_workspace);
}

export async function addManagedGatewayWorkspace(
  port: number,
  payload: AddManagedGatewayWorkspaceRequest,
): Promise<GatewayManagedWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayManagedWorkspaceList>>(
      port,
      "/api/gateway/managed-workspaces",
      {
        method: "POST",
        timeoutMs: LIFECYCLE_REQUEST_TIMEOUT_MS,
        body: JSON.stringify({
          ...payload,
          create_directory: payload.create_directory ?? false,
        }),
      },
    ),
  );
}

export async function addRemoteGatewayConnection(
  port: number,
  payload: AddRemoteGatewayConnectionRequest,
): Promise<GatewayWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      "/api/gateway/remote-gateways",
      {
        method: "POST",
        timeoutMs: LIFECYCLE_REQUEST_TIMEOUT_MS,
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function removeGatewayWorkspace(
  port: number,
  workspaceId: string,
  groupRequest?: GatewayRuntimeGroupRequest,
  force = false,
): Promise<GatewayWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      `${gatewayWorkspacePath(workspaceId)}?force=${force}`,
      {
        headers: workspaceHeader(workspaceId),
        method: "DELETE",
        timeoutMs: LIFECYCLE_REQUEST_TIMEOUT_MS,
        body: groupRequest ? JSON.stringify(groupRequest) : undefined,
      },
    ),
  );
}

export async function renameGatewayWorkspace(
  port: number,
  workspaceId: string,
  payload: UpdateGatewayWorkspaceRequest,
): Promise<GatewayWorkspaceList> {
  return updateGatewayWorkspace(port, workspaceId, payload);
}

export async function updateGatewayWorkspace(
  port: number,
  workspaceId: string,
  payload: UpdateGatewayWorkspaceRequest,
): Promise<GatewayWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      `${gatewayWorkspacePath(workspaceId)}`,
      {
        headers: workspaceHeader(workspaceId),
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
      `${gatewayWorkspacePath(workspaceId)}/reconnect`,
      { headers: workspaceHeader(workspaceId), method: "POST", timeoutMs: LIFECYCLE_REQUEST_TIMEOUT_MS },
    ),
  );
}

export async function reconnectRemoteGatewayConnection(port: number, gatewayConnectionId: string): Promise<GatewayWorkspaceList> {
  return unwrapApiData(await requestJson<APIResponse<GatewayWorkspaceList>>(
    port, `/api/gateway/remote-gateways/${encodeURIComponent(gatewayConnectionId)}/reconnect`,
    { method: "POST", timeoutMs: LIFECYCLE_REQUEST_TIMEOUT_MS },
  ));
}

export async function removeRemoteGatewayConnection(port: number, gatewayConnectionId: string): Promise<GatewayWorkspaceList> {
  return unwrapApiData(await requestJson<APIResponse<GatewayWorkspaceList>>(
    port, `/api/gateway/remote-gateways/${encodeURIComponent(gatewayConnectionId)}`,
    { method: "DELETE", timeoutMs: LIFECYCLE_REQUEST_TIMEOUT_MS },
  ));
}

export async function safeRestartManagedGatewayWorkspaceBackend(
  port: number,
  workspaceId: string,
  groupRequest?: GatewayRuntimeGroupRequest,
): Promise<GatewayRuntimeRestartResult> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayRuntimeRestartResult>>(
      port,
      `${gatewayWorkspacePath(workspaceId)}/runtime/restart-safe`,
      { headers: workspaceHeader(workspaceId), method: "POST", body: groupRequest === undefined ? undefined : JSON.stringify(groupRequest), timeoutMs: LIFECYCLE_REQUEST_TIMEOUT_MS },
    ),
  );
}

export async function startManagedGatewayWorkspaceBackend(
  port: number,
  workspaceId: string,
  groupRequest?: GatewayRuntimeGroupRequest,
): Promise<GatewayRuntimeStateResult> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayRuntimeStateResult>>(
      port,
      `${gatewayWorkspacePath(workspaceId)}/runtime/start`,
      { headers: workspaceHeader(workspaceId), method: "POST", body: groupRequest === undefined ? undefined : JSON.stringify(groupRequest), timeoutMs: LIFECYCLE_REQUEST_TIMEOUT_MS },
    ),
  );
}

export async function stopManagedGatewayWorkspaceBackend(
  port: number,
  workspaceId: string,
  groupRequest?: GatewayRuntimeGroupRequest,
): Promise<GatewayRuntimeStateResult> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayRuntimeStateResult>>(
      port,
      `${gatewayWorkspacePath(workspaceId)}/runtime/stop`,
      { headers: workspaceHeader(workspaceId), method: "POST", body: groupRequest === undefined ? undefined : JSON.stringify(groupRequest), timeoutMs: LIFECYCLE_REQUEST_TIMEOUT_MS },
    ),
  );
}

export async function forceRestartManagedGatewayWorkspaceBackend(
  port: number,
  workspaceId: string,
  groupRequest?: GatewayRuntimeGroupRequest,
): Promise<GatewayRuntimeRestartResult> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayRuntimeRestartResult>>(
      port,
      `${gatewayWorkspacePath(workspaceId)}/runtime/restart-force`,
      { headers: workspaceHeader(workspaceId), method: "POST", body: groupRequest === undefined ? undefined : JSON.stringify(groupRequest), timeoutMs: LIFECYCLE_REQUEST_TIMEOUT_MS },
    ),
  );
}

export async function probeExternalGatewayWorkspace(
  port: number,
  workspaceId: string,
): Promise<GatewayWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      `${gatewayWorkspacePath(workspaceId)}/probe`,
      { headers: workspaceHeader(workspaceId), method: "POST", timeoutMs: LIFECYCLE_REQUEST_TIMEOUT_MS },
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

export async function listGatewayUiAssets(port: number): Promise<GatewayUiAsset[]> {
  return normalizePageResult<GatewayUiAsset>(
    unwrapApiData(
      await requestJson<APIResponse<GatewayUiAssetList>>(
        port,
        "/api/gateway/ui-assets",
      ),
    ),
    "Gateway UI 资源列表",
  ).items;
}

/**
 * Gateway 背景图资源的上限，与后端 app/gateway/theme/assets.py 的 MAX_UI_ASSET_BYTES 一致。
 * 后端会先读完整个请求体再校验，数百 MB 的图片必然失败却要先传输一遍；空文件同样注定被拒。
 * 因此在发请求前按同一上限拦住并给出可读错误，绝不把注定失败的载荷发出去。
 */
const MAX_GATEWAY_UI_ASSET_BYTES = 20 * 1024 * 1024;

function assertUiAssetSize(file: File): void {
  if (file.size === 0) {
    throw new Error(t("gateway.theme.assetEmpty"));
  }
  if (file.size > MAX_GATEWAY_UI_ASSET_BYTES) {
    const limitMiB = MAX_GATEWAY_UI_ASSET_BYTES / 1024 / 1024;
    throw new Error(t("gateway.theme.assetTooLarge",
      formatNumber(file.size / 1024 / 1024, { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
      formatNumber(limitMiB)));
  }
}

export async function uploadGatewayUiAsset(
  port: number,
  file: File,
): Promise<GatewayUiAsset> {
  assertUiAssetSize(file);
  const body = new FormData();
  body.append("file", file);
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUiAsset>>(
      port,
      "/api/gateway/ui-assets",
      { method: "POST", body },
    ),
  );
}

export async function deleteGatewayUiAsset(port: number, assetId: string): Promise<GatewayUiAsset[]> {
  return normalizePageResult<GatewayUiAsset>(
    unwrapApiData(
      await requestJson<APIResponse<GatewayUiAssetList>>(
        port,
        `/api/gateway/ui-assets/${encodeURIComponent(assetId)}`,
        { method: "DELETE" },
      ),
    ),
    "Gateway UI 资源列表",
  ).items;
}

export async function browseGatewayLocalDirectories(
  port: number,
  path?: string | null,
  gatewayConnectionId?: string | null,
  basePath?: string | null,
): Promise<GatewayDirectoryList> {
  const query = new URLSearchParams();
  if (path?.trim()) {
    query.set("path", path.trim());
  }
  if (gatewayConnectionId) {
    query.set("gateway_connection_id", gatewayConnectionId);
  }
  if (basePath) query.set("base_path", basePath);
  const suffix = query.toString();
  const requestPath = `/api/gateway/local-directories${suffix ? `?${suffix}` : ""}`;
  const requestListing = async () =>
    unwrapApiData(
      await requestJson<APIResponse<GatewayDirectoryList>>(port, requestPath),
    );
  try {
    return await requestListing();
  } catch (error) {
    if (!(error instanceof HttpRequestError) || error.status !== 503) throw error;
    // Vite 开发代理偶发在 Gateway 可用时返回一次 503；只对幂等目录读取重试一次，
    // 第二次仍失败则原样抛出 HttpRequestError，绝不再放大流量或吞成空结果。
    return await requestListing();
  }
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

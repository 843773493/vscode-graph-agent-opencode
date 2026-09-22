import type {
  AddManagedGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  ActivateGatewayWorkspaceResultDTO,
  APIResponse,
  GatewayInboundAccessList,
  GatewayDeviceConnectionList,
  GatewayDeviceAccessAddressList,
  CreatedGatewayDeviceConnection,
  GatewayWorkspaceList,
  GatewayRuntimeRestartResult,
  GatewayRuntimeStateResult,
  GatewayHealth,
  GatewayDiagnostics,
  DevelopmentRuntimeRestartResult,
  GatewayDirectoryList,
  GatewayManagedWorkspaceList,
  UpdateGatewayWorkspaceRequest,
  ReorderGatewayWorkspacesRequest,
  SshConnectionOptionList,
  WebUiSettings,
  WebUiSettingsUpdate,
  GatewayThemeCatalog,
  GatewayUiAsset,
  GatewayUiAssetList,
  GatewaySessionSearchResults,
  GenerationRun,
  GenerationRunList,
  GeneratorPlacementPreview,
  SessionGeneratorDefinition,
  SessionGeneratorList,
  WorkspaceNavigationTree,
  CreateGatewayPortForwardRequest,
  ChangeGatewayPortForwardLocalPortRequest,
  ChangeGatewayPortForwardLabelRequest,
  GatewayPortForwardList,
  GatewayResourceList,
  GeneratorDefinitionCreateRequest,
  GeneratorDefinitionUpdateRequest,
  GeneratorPlacementPreviewRequest,
  WorkspaceFolderCreateRequest,
  WorkspaceNavigationNodeUpdateRequest,
  WorkspaceNavigationPlacementRequest,
} from "./types/backend";
import { normalizeWebUiSettings } from "./state/uiSettings/preferences";
import {
  DEFAULT_API_REQUEST_TIMEOUT_MS,
  HttpRequestError,
  normalizePageResult,
  requestJson,
  unwrapApiData,
} from "./api/http";

export async function getGatewayHealth(port: number): Promise<GatewayHealth> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayHealth>>(
      port,
      "/api/gateway/health",
    ),
  );
}

export async function getGatewayDiagnostics(
  port: number,
  options: {
    gatewayConnectionId?: string | null;
    workspaceId?: string | null;
    logId?: string | null;
    tailLines?: number;
  } = {},
): Promise<GatewayDiagnostics> {
  const params = new URLSearchParams();
  if (options.gatewayConnectionId) {
    params.set("gateway_connection_id", options.gatewayConnectionId);
  }
  if (options.workspaceId) params.set("workspace_id", options.workspaceId);
  if (options.logId) params.set("log_id", options.logId);
  if (options.tailLines) params.set("tail_lines", String(options.tailLines));
  const query = params.toString();
  return unwrapApiData(
    await requestJson<APIResponse<GatewayDiagnostics>>(
      port,
      `/api/gateway/diagnostics${query ? `?${query}` : ""}`,
      { timeoutMs: DEFAULT_API_REQUEST_TIMEOUT_MS },
    ),
  );
}

export async function restartDevelopmentRuntime(
  port: number,
): Promise<DevelopmentRuntimeRestartResult> {
  return unwrapApiData(
    await requestJson<APIResponse<DevelopmentRuntimeRestartResult>>(
      port,
      "/api/gateway/runtime/restart-development",
      { method: "POST" },
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

export async function listWorkspacePortForwards(
  port: number,
  workspaceId: string,
): Promise<GatewayPortForwardList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayPortForwardList>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/port-forwards`,
    ),
  );
}

export async function createWorkspacePortForward(
  port: number,
  workspaceId: string,
  payload: CreateGatewayPortForwardRequest,
): Promise<GatewayPortForwardList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayPortForwardList>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/port-forwards`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  );
}

export async function deleteWorkspacePortForward(
  port: number,
  workspaceId: string,
  forwardId: string,
): Promise<GatewayPortForwardList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayPortForwardList>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/port-forwards/${encodeURIComponent(forwardId)}`,
      { method: "DELETE" },
    ),
  );
}

export async function reconnectWorkspacePortForward(
  port: number,
  workspaceId: string,
  forwardId: string,
): Promise<GatewayPortForwardList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayPortForwardList>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/port-forwards/${encodeURIComponent(forwardId)}/reconnect`,
      { method: "POST", body: "{}" },
    ),
  );
}

export async function changeWorkspacePortForwardLocalPort(
  port: number,
  workspaceId: string,
  forwardId: string,
  payload: ChangeGatewayPortForwardLocalPortRequest,
): Promise<GatewayPortForwardList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayPortForwardList>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/port-forwards/${encodeURIComponent(forwardId)}/local-port`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  );
}

export async function changeWorkspacePortForwardLabel(
  port: number,
  workspaceId: string,
  forwardId: string,
  payload: ChangeGatewayPortForwardLabelRequest,
): Promise<GatewayPortForwardList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayPortForwardList>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/port-forwards/${encodeURIComponent(forwardId)}/label`,
      {
        method: "PATCH",
        body: JSON.stringify(payload),
      },
    ),
  );
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

export async function searchGatewaySessionCatalog(
  port: number,
  query: string,
  signal?: AbortSignal,
): Promise<GatewaySessionSearchResults> {
  const params = new URLSearchParams({ query, limit_per_workspace: "50" });
  return unwrapApiData(
    await requestJson<APIResponse<GatewaySessionSearchResults>>(
      port,
      `/api/gateway/session-catalog/search?${params.toString()}`,
      { timeoutMs: DEFAULT_API_REQUEST_TIMEOUT_MS, signal },
    ),
  );
}

export async function listSessionGenerators(
  port: number,
): Promise<SessionGeneratorList> {
  return unwrapApiData(
    await requestJson<APIResponse<SessionGeneratorList>>(
      port,
      "/api/gateway/session-generators",
    ),
  );
}

export async function createSessionGenerator(
  port: number,
  payload: GeneratorDefinitionCreateRequest,
): Promise<SessionGeneratorDefinition> {
  return unwrapApiData(
    await requestJson<APIResponse<SessionGeneratorDefinition>>(
      port,
      "/api/gateway/session-generators",
      { method: "POST", body: JSON.stringify(payload) },
    ),
  );
}

export async function runSessionGenerator(
  port: number,
  generatorId: string,
): Promise<GenerationRun> {
  return unwrapApiData(
    await requestJson<APIResponse<GenerationRun>>(
      port,
      `/api/gateway/session-generators/${encodeURIComponent(generatorId)}/run`,
      { method: "POST", body: "{}", timeoutMs: DEFAULT_API_REQUEST_TIMEOUT_MS },
    ),
  );
}

export async function listSessionGeneratorRuns(
  port: number,
  generatorId: string,
): Promise<GenerationRunList> {
  return unwrapApiData(
    await requestJson<APIResponse<GenerationRunList>>(
      port,
      `/api/gateway/session-generators/${encodeURIComponent(generatorId)}/runs`,
    ),
  );
}

export async function updateSessionGenerator(
  port: number,
  generatorId: string,
  payload: GeneratorDefinitionUpdateRequest,
): Promise<SessionGeneratorDefinition> {
  return unwrapApiData(
    await requestJson<APIResponse<SessionGeneratorDefinition>>(
      port,
      `/api/gateway/session-generators/${encodeURIComponent(generatorId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  );
}

export async function deleteSessionGenerator(
  port: number,
  generatorId: string,
): Promise<SessionGeneratorDefinition> {
  return unwrapApiData(
    await requestJson<APIResponse<SessionGeneratorDefinition>>(
      port,
      `/api/gateway/session-generators/${encodeURIComponent(generatorId)}`,
      { method: "DELETE" },
    ),
  );
}

export async function previewSessionGeneratorPlacement(
  port: number,
  payload: GeneratorPlacementPreviewRequest,
): Promise<GeneratorPlacementPreview> {
  return unwrapApiData(
    await requestJson<APIResponse<GeneratorPlacementPreview>>(
      port,
      "/api/gateway/session-generators/preview-placement",
      { method: "POST", body: JSON.stringify(payload) },
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
  connectionId: string,
): Promise<GatewayDeviceConnectionList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayDeviceConnectionList>>(
      port,
      `/api/gateway/device-connections/${encodeURIComponent(connectionId)}`,
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
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/activate`,
      { method: "POST", signal },
    ),
  );
  return result.active_workspace_id;
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
        body: JSON.stringify({
          ...payload,
          create_directory: payload.create_directory ?? false,
        }),
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
      "/api/gateway/remote-gateways",
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

export async function safeRestartManagedGatewayWorkspaceBackend(
  port: number,
  workspaceId: string,
): Promise<GatewayRuntimeRestartResult> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayRuntimeRestartResult>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/runtime/restart-safe`,
      { method: "POST" },
    ),
  );
}

export async function startManagedGatewayWorkspaceBackend(
  port: number,
  workspaceId: string,
): Promise<GatewayRuntimeStateResult> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayRuntimeStateResult>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/runtime/start`,
      { method: "POST" },
    ),
  );
}

export async function stopManagedGatewayWorkspaceBackend(
  port: number,
  workspaceId: string,
): Promise<GatewayRuntimeStateResult> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayRuntimeStateResult>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/runtime/stop`,
      { method: "POST" },
    ),
  );
}

export async function forceRestartManagedGatewayWorkspaceBackend(
  port: number,
  workspaceId: string,
): Promise<GatewayRuntimeRestartResult> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayRuntimeRestartResult>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/runtime/restart-force`,
      { method: "POST" },
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
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/probe`,
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
  return normalizeWebUiSettings(
    unwrapApiData(
      await requestJson<APIResponse<WebUiSettings>>(
        port,
        "/api/gateway/ui-settings",
      ),
    ),
  );
}

export async function updateGatewayUiSettings(
  port: number,
  payload: WebUiSettingsUpdate,
): Promise<WebUiSettings> {
  return normalizeWebUiSettings(
    unwrapApiData(
      await requestJson<APIResponse<WebUiSettings>>(
        port,
        "/api/gateway/ui-settings",
        {
          method: "PUT",
          body: JSON.stringify(payload),
        },
      ),
    ),
  );
}

export async function getGatewayThemes(port: number): Promise<GatewayThemeCatalog> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayThemeCatalog>>(port, "/api/gateway/themes"),
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
 * 因此在发请求前按同一上限拦住并给出可读中文错误，绝不把注定失败的载荷发出去。
 */
const MAX_GATEWAY_UI_ASSET_BYTES = 20 * 1024 * 1024;

function assertUiAssetSize(file: File): void {
  if (file.size === 0) {
    throw new Error("背景图片内容为空");
  }
  if (file.size > MAX_GATEWAY_UI_ASSET_BYTES) {
    const limitMiB = MAX_GATEWAY_UI_ASSET_BYTES / 1024 / 1024;
    throw new Error(
      `背景图片 ${(file.size / 1024 / 1024).toFixed(1)} MiB 超过 ${limitMiB} MiB 限制`,
    );
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
): Promise<GatewayDirectoryList> {
  const query = new URLSearchParams();
  if (path?.trim()) {
    query.set("path", path.trim());
  }
  if (gatewayConnectionId) {
    query.set("gateway_connection_id", gatewayConnectionId);
  }
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

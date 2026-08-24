import type {
  AddManagedGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  AcquireGatewayUserRequest,
  ActivateGatewayWorkspaceResultDTO,
  APIResponse,
  CreateGatewayGuestRequest,
  CreateGatewayUserRequest,
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
  GatewayUser,
  GatewayUserAccess,
  GatewayUserList,
  GatewayUserViewState,
  GatewayUserViewStateUpdateRequest,
  GeneratorDefinitionCreateRequest,
  GeneratorDefinitionUpdateRequest,
  GeneratorPlacementPreviewRequest,
  WorkspaceFolderCreateRequest,
  WorkspaceNavigationNodeUpdateRequest,
  WorkspaceNavigationPlacementRequest,
} from "./types/backend";
import { HttpRequestError, requestJson, unwrapApiData } from "./api";
import type { CreatableSessionConnectionKind } from "./types/frontend";

const WEB_GUEST_REQUEST: CreateGatewayGuestRequest = {
  tracking: { source: "web" },
};

export interface CreatedSessionConnection {
  kind: CreatableSessionConnectionKind;
  resourceId: string;
}

interface ManagerResourceResponse {
  data?: Record<string, unknown>;
}

interface SessionConnectionCreateRequest {
  service: string;
  path: string;
  payload: Record<string, unknown>;
  idField: string;
}

const SESSION_CONNECTION_CREATE_REQUESTS: Record<
  CreatableSessionConnectionKind,
  (sessionId: string) => SessionConnectionCreateRequest
> = {
  terminal: (sessionId) => ({
    service: "terminal-manager",
    path: "api/terminals",
    payload: {
      session_id: sessionId,
      title: "用户终端",
    },
    idField: "terminal_id",
  }),
  browser: (sessionId) => ({
    service: "browser-manager",
    path: "api/browsers",
    payload: {
      session_id: sessionId,
      title: "用户浏览器",
      url: "about:blank",
      viewport: { width: 1280, height: 800 },
    },
    idField: "browser_id",
  }),
};

export async function createSessionConnection(
  port: number,
  workspaceId: string,
  sessionId: string,
  kind: CreatableSessionConnectionKind,
): Promise<CreatedSessionConnection> {
  const encodedWorkspaceId = encodeURIComponent(workspaceId);
  const request = SESSION_CONNECTION_CREATE_REQUESTS[kind](sessionId);
  const response = await requestJson<ManagerResourceResponse>(
    port,
    `/api/gateway/workspaces/${encodedWorkspaceId}/${request.service}/${request.path}`,
    {
      method: "POST",
      body: JSON.stringify(request.payload),
    },
  );
  const resourceId = response.data?.[request.idField];
  if (typeof resourceId !== "string" || !resourceId) {
    throw new Error(`${request.service} 创建响应缺少 ${request.idField}`);
  }
  return { kind, resourceId };
}

export async function getGatewayHealth(port: number): Promise<GatewayHealth> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayHealth>>(
      port,
      "/api/gateway/health",
    ),
  );
}

export async function getCurrentGatewayUser(
  port: number,
): Promise<GatewayUserAccess> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUserAccess>>(
      port,
      "/api/gateway/users/current",
    ),
  );
}

export async function ensureGatewayUserAccess(
  port: number,
): Promise<GatewayUserAccess> {
  try {
    return await getCurrentGatewayUser(port);
  } catch (error: unknown) {
    if (!(error instanceof HttpRequestError) || error.status !== 401) throw error;
    return unwrapApiData(
      await requestJson<APIResponse<GatewayUserAccess>>(
        port,
        "/api/gateway/users/guest",
        { method: "POST", body: JSON.stringify(WEB_GUEST_REQUEST) },
      ),
    );
  }
}

export async function acquireGatewayGuest(
  port: number,
): Promise<GatewayUserAccess> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUserAccess>>(
      port,
      "/api/gateway/users/guest",
      { method: "POST", body: JSON.stringify(WEB_GUEST_REQUEST) },
    ),
  );
}

export async function listGatewayUsers(port: number): Promise<GatewayUserList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUserList>>(port, "/api/gateway/users"),
  );
}

export async function createGatewayUser(
  port: number,
  payload: CreateGatewayUserRequest,
): Promise<GatewayUser> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUser>>(port, "/api/gateway/users", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

export async function deleteGatewayUser(port: number, userId: string): Promise<void> {
  await requestJson<APIResponse<{ user_id: string }>>(
    port,
    `/api/gateway/users/${encodeURIComponent(userId)}`,
    { method: "DELETE" },
  );
}

async function acquireGatewayUser(
  port: number,
  userId: string,
  path: "access" | "takeover",
  clientLabel?: string,
): Promise<GatewayUserAccess> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUserAccess>>(
      port,
      `/api/gateway/users/${encodeURIComponent(userId)}/${path}`,
      {
        method: "POST",
        body: JSON.stringify({
          client_label: clientLabel ?? null,
        } satisfies AcquireGatewayUserRequest),
      },
    ),
  );
}

export function selectGatewayUser(
  port: number,
  userId: string,
  clientLabel?: string,
): Promise<GatewayUserAccess> {
  return acquireGatewayUser(port, userId, "access", clientLabel);
}

export function takeoverGatewayUser(
  port: number,
  userId: string,
  clientLabel?: string,
): Promise<GatewayUserAccess> {
  return acquireGatewayUser(port, userId, "takeover", clientLabel);
}

export async function heartbeatGatewayUser(
  port: number,
): Promise<GatewayUserAccess> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUserAccess>>(
      port,
      "/api/gateway/users/current/heartbeat",
      { method: "POST" },
    ),
  );
}

export async function getGatewayUserViewState(
  port: number,
  workspaceId: string,
  sessionId: string,
): Promise<GatewayUserViewState | null> {
  const response = await requestJson<APIResponse<GatewayUserViewState | null>>(
    port,
    `/api/gateway/users/current/view-state?workspace_id=${encodeURIComponent(workspaceId)}&session_id=${encodeURIComponent(sessionId)}`,
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
      `/api/gateway/users/current/view-state?workspace_id=${encodeURIComponent(workspaceId)}&session_id=${encodeURIComponent(sessionId)}`,
      { method: "PUT", body: JSON.stringify(payload) },
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
      { timeoutMs: 15000 },
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
): Promise<GatewayWorkspaceList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayWorkspaceList>>(
      port,
      "/api/gateway/workspaces",
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
      { timeoutMs: 15000, signal },
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
      { method: "POST", body: "{}", timeoutMs: 15000 },
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
): Promise<string> {
  const result = unwrapApiData(
    await requestJson<APIResponse<ActivateGatewayWorkspaceResultDTO>>(
      port,
      `/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/activate`,
      { method: "POST" },
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

export async function getGatewayThemes(port: number): Promise<GatewayThemeCatalog> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayThemeCatalog>>(port, "/api/gateway/themes"),
  );
}

export async function listGatewayUiAssets(port: number): Promise<GatewayUiAsset[]> {
  const result = unwrapApiData(
    await requestJson<APIResponse<GatewayUiAssetList>>(
      port,
      "/api/gateway/ui-assets",
    ),
  );
  return result.items;
}

export async function uploadGatewayUiAsset(
  port: number,
  file: File,
): Promise<GatewayUiAsset> {
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
  const result = unwrapApiData(
    await requestJson<APIResponse<GatewayUiAssetList>>(
      port,
      `/api/gateway/ui-assets/${encodeURIComponent(assetId)}`,
      { method: "DELETE" },
    ),
  );
  return result.items;
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
    // TODO: Vite 开发代理偶发在 Gateway 可用时返回一次 503；仅对幂等目录读取重试一次。
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

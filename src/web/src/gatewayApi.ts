import type {
  AddLocalGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  APIResponse,
  GatewayInboundAccessList,
  GatewayWorkspaceList,
  GatewayRuntimeRestartResult,
  GatewayHealth,
  GatewayDirectoryList,
  UpdateGatewayWorkspaceRequest,
  ReorderGatewayWorkspacesRequest,
  SshConnectionOptionList,
  WebUiSettings,
  WebUiSettingsUpdate,
  GatewaySessionSearchResults,
  GenerationRun,
  GenerationRunList,
  GeneratorPlacementPreview,
  SessionGeneratorDefinition,
  SessionGeneratorList,
  WorkspaceNavigationTree,
} from "./types/backend";
import { requestJson, unwrapApiData } from "./api";

export async function getGatewayHealth(port: number): Promise<GatewayHealth> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayHealth>>(
      port,
      "/api/gateway/health",
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
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceNavigationTree>>(
      port,
      "/api/gateway/workspace-navigation/folders",
      {
        method: "POST",
        body: JSON.stringify({ name, parent_node_id: parentNodeId ?? null }),
      },
    ),
  );
}

export async function renameWorkspaceNavigationFolder(
  port: number,
  nodeId: string,
  name: string,
): Promise<WorkspaceNavigationTree> {
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceNavigationTree>>(
      port,
      `/api/gateway/workspace-navigation/nodes/${encodeURIComponent(nodeId)}`,
      { method: "PATCH", body: JSON.stringify({ name }) },
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

export async function moveWorkspaceNavigationNode(
  port: number,
  nodeId: string,
  parentNodeId?: string | null,
): Promise<WorkspaceNavigationTree> {
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceNavigationTree>>(
      port,
      `/api/gateway/workspace-navigation/nodes/${encodeURIComponent(nodeId)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ parent_node_id: parentNodeId ?? null }),
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
  payload: Record<string, unknown>,
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
  payload: Record<string, unknown>,
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
  payload: Record<string, unknown>,
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

export async function browseGatewayLocalDirectories(
  port: number,
  path?: string | null,
): Promise<GatewayDirectoryList> {
  const query = new URLSearchParams();
  if (path?.trim()) {
    query.set("path", path.trim());
  }
  const suffix = query.toString();
  return unwrapApiData(
    await requestJson<APIResponse<GatewayDirectoryList>>(
      port,
      `/api/gateway/local-directories${suffix ? `?${suffix}` : ""}`,
    ),
  );
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

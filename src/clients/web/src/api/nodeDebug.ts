import type {
  APIResponse,
  NodeDebugActionRequest,
  NodeDebugCapabilities,
  NodeDebugConfiguration,
  NodeDebugConfigurationActivateRequest,
  NodeDebugConfigurationCopyRequest,
  NodeDebugConfigurationCreateRequest,
  NodeDebugConfigurationImportRequest,
  NodeDebugConfigurationUpdateRequest,
  NodeDebugStartRequest,
  NodeDebugState,
} from "../types/backend";
import { requestJson, unwrapApiData, workspaceHeader } from "./http";

const NODE_DEBUG_TIMEOUT_MS = 15000;

export async function getNodeDebugCapabilities(
  port: number,
  workspaceId?: string | null,
): Promise<NodeDebugCapabilities> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugCapabilities>>(
      port,
      "/api/v1/debug/node/capabilities",
      {
        headers: workspaceHeader(workspaceId),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function getNodeDebugState(
  port: number,
  sessionId: string,
  threadId: string,
  workspaceId?: string | null,
): Promise<NodeDebugState> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugState>>(
      port,
      `/api/v1/debug/node?session_id=${encodeURIComponent(sessionId)}&thread_id=${encodeURIComponent(threadId)}`,
      {
        headers: workspaceHeader(workspaceId),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function startNodeDebug(
  port: number,
  payload: NodeDebugStartRequest,
  workspaceId?: string | null,
): Promise<NodeDebugState> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugState>>(
      port,
      "/api/v1/debug/node/start",
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function createNodeDebugConfiguration(
  port: number,
  payload: NodeDebugConfigurationCreateRequest,
  workspaceId?: string | null,
): Promise<NodeDebugState> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugState>>(
      port,
      "/api/v1/debug/node/configurations",
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function updateNodeDebugConfiguration(
  port: number,
  configurationId: string,
  payload: NodeDebugConfigurationUpdateRequest,
  workspaceId?: string | null,
): Promise<NodeDebugState> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugState>>(
      port,
      `/api/v1/debug/node/configurations/${encodeURIComponent(configurationId)}`,
      {
        method: "PUT",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function activateNodeDebugConfiguration(
  port: number,
  configurationId: string,
  payload: NodeDebugConfigurationActivateRequest,
  workspaceId?: string | null,
): Promise<NodeDebugState> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugState>>(
      port,
      `/api/v1/debug/node/configurations/${encodeURIComponent(configurationId)}/activate`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function deleteNodeDebugConfiguration(
  port: number,
  sessionId: string,
  threadId: string,
  configurationId: string,
  workspaceId?: string | null,
): Promise<NodeDebugState> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugState>>(
      port,
      `/api/v1/debug/node/configurations/${encodeURIComponent(configurationId)}?session_id=${encodeURIComponent(sessionId)}&thread_id=${encodeURIComponent(threadId)}`,
      {
        method: "DELETE",
        headers: workspaceHeader(workspaceId),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function getNodeDebugConfiguration(
  port: number,
  sessionId: string,
  threadId: string,
  configurationId: string,
  workspaceId?: string | null,
): Promise<NodeDebugConfiguration> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugConfiguration>>(
      port,
      `/api/v1/debug/node/configurations/${encodeURIComponent(configurationId)}?session_id=${encodeURIComponent(sessionId)}&thread_id=${encodeURIComponent(threadId)}`,
      {
        headers: workspaceHeader(workspaceId),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function importNodeDebugConfiguration(
  port: number,
  payload: NodeDebugConfigurationImportRequest,
  workspaceId?: string | null,
): Promise<NodeDebugState> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugState>>(
      port,
      "/api/v1/debug/node/configurations/import",
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function copyNodeDebugConfiguration(
  port: number,
  configurationId: string,
  payload: NodeDebugConfigurationCopyRequest,
  workspaceId?: string | null,
): Promise<NodeDebugConfiguration> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugConfiguration>>(
      port,
      `/api/v1/debug/node/configurations/${encodeURIComponent(configurationId)}/copy`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function applyNodeDebugAction(
  port: number,
  payload: NodeDebugActionRequest,
  workspaceId?: string | null,
): Promise<NodeDebugState> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugState>>(
      port,
      "/api/v1/debug/node/action",
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

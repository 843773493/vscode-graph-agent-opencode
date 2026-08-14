import type {
  APIResponse,
  NodeDebugActionRequest,
  NodeDebugCapabilities,
  NodeDebugConfiguration,
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
  workspaceId?: string | null,
): Promise<NodeDebugState> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugState>>(
      port,
      `/api/v1/debug/node?session_id=${encodeURIComponent(sessionId)}`,
      {
        headers: workspaceHeader(workspaceId),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function startNodeDebug(
  port: number,
  payload: {
    session_id: string;
    configuration_id?: string | null;
    path: string;
    working_directory?: string | null;
    launch_profile_name?: string | null;
    args?: string[];
    breakpoints?: Array<{
      path: string;
      line: number;
      column?: number;
      condition?: string | null;
      hit_condition?: number | null;
      log_message?: string | null;
    }>;
  },
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
  payload: {
    session_id: string;
    name: string;
    script_path?: string | null;
    working_directory?: string;
    launch_profile_name?: string | null;
    args?: string[];
    activate?: boolean;
  },
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
  payload: {
    session_id: string;
    name: string;
    script_path?: string | null;
    working_directory?: string;
    launch_profile_name?: string | null;
    args?: string[];
    breakpoints?: Array<{
      path: string;
      line: number;
      column?: number;
      condition?: string | null;
      hit_condition?: number | null;
      log_message?: string | null;
    }>;
  },
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
  sessionId: string,
  configurationId: string,
  workspaceId?: string | null,
): Promise<NodeDebugState> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugState>>(
      port,
      `/api/v1/debug/node/configurations/${encodeURIComponent(configurationId)}/activate`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify({ session_id: sessionId }),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function deleteNodeDebugConfiguration(
  port: number,
  sessionId: string,
  configurationId: string,
  workspaceId?: string | null,
): Promise<NodeDebugState> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugState>>(
      port,
      `/api/v1/debug/node/configurations/${encodeURIComponent(configurationId)}?session_id=${encodeURIComponent(sessionId)}`,
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
  configurationId: string,
  workspaceId?: string | null,
): Promise<NodeDebugConfiguration> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugConfiguration>>(
      port,
      `/api/v1/debug/node/configurations/${encodeURIComponent(configurationId)}?session_id=${encodeURIComponent(sessionId)}`,
      {
        headers: workspaceHeader(workspaceId),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function importNodeDebugConfiguration(
  port: number,
  sessionId: string,
  configuration: NodeDebugConfiguration,
  workspaceId?: string | null,
): Promise<NodeDebugState> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugState>>(
      port,
      "/api/v1/debug/node/configurations/import",
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify({ session_id: sessionId, configuration, activate: false }),
        timeoutMs: NODE_DEBUG_TIMEOUT_MS,
      },
    ),
  );
}

export async function copyNodeDebugConfiguration(
  port: number,
  sourceSessionId: string,
  targetSessionId: string,
  configurationId: string,
  workspaceId?: string | null,
): Promise<NodeDebugConfiguration> {
  return unwrapApiData(
    await requestJson<APIResponse<NodeDebugConfiguration>>(
      port,
      `/api/v1/debug/node/configurations/${encodeURIComponent(configurationId)}/copy`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify({
          source_session_id: sourceSessionId,
          target_session_id: targetSessionId,
          activate: false,
        }),
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

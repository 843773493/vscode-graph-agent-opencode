import type {
  APIResponse,
  NodeDebugActionRequest,
  NodeDebugState,
} from "../types/backend";
import { requestJson, unwrapApiData, workspaceHeader } from "./http";

const NODE_DEBUG_TIMEOUT_MS = 15000;

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
    path: string;
    args?: string[];
    breakpoints?: Array<{ path: string; line: number; column?: number }>;
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

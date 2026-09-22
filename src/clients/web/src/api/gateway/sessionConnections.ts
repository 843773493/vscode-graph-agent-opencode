import type { CreatableSessionConnectionKind } from "../../types/frontend";
import { requestJson } from "../http";

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

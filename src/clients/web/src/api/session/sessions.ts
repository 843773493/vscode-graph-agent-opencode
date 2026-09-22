import type {
  APIResponse,
  ChildThreadList,
  ChildThreadSummary,
  CursorPage,
  DeleteSessionResult,
  Session,
  SessionCompactResult,
  SessionInformationSnapshot,
  SessionUpdateRequest,
} from "../../types/backend";
import { parseChildThreadStatus } from "../../types/protocol";
import {
  DEFAULT_API_REQUEST_TIMEOUT_MS,
  normalizePageResult,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "../http";

export const DEFAULT_SESSION_TITLE = "新会话";

function parseChildThreadList(value: unknown): ChildThreadList {
  if (!value || typeof value !== "object") {
    throw new Error("child thread 协议响应必须是对象");
  }
  const record = value as {
    parent_session_id?: unknown;
    items?: unknown;
    total?: unknown;
  };
  if (
    typeof record.parent_session_id !== "string"
    || !Array.isArray(record.items)
    || typeof record.total !== "number"
  ) {
    throw new Error("child thread 协议响应缺少 parent_session_id、items 或 total");
  }
  const items = record.items.map((item, index): ChildThreadSummary => {
    if (!item || typeof item !== "object") {
      throw new Error(`child thread 协议项 ${index} 必须是对象`);
    }
    const child = item as Record<string, unknown>;
    return {
      ...child,
      status: parseChildThreadStatus(child.status),
    } as ChildThreadSummary;
  });
  return {
    parent_session_id: record.parent_session_id,
    items,
    total: record.total,
  };
}

export async function listSessions(
  port: number,
  workspaceId?: string | null,
): Promise<CursorPage<Session>> {
  const data = await requestJson<APIResponse<CursorPage<Session>>>(
    port,
    "/api/v1/sessions",
    workspaceId
      ? {
          headers: workspaceHeader(workspaceId),
          timeoutMs: DEFAULT_API_REQUEST_TIMEOUT_MS,
        }
      : { timeoutMs: DEFAULT_API_REQUEST_TIMEOUT_MS },
  );
  return normalizePageResult<Session>(unwrapApiData(data), "会话列表");
}

export async function getSession(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<Session> {
  return unwrapApiData(
    await requestJson<APIResponse<Session>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      workspaceId ? { headers: workspaceHeader(workspaceId) } : undefined,
    ),
  );
}

export async function getSessionInformation(
  port: number,
  sessionId: string,
  workspaceId: string,
): Promise<SessionInformationSnapshot> {
  return unwrapApiData(
    await requestJson<APIResponse<SessionInformationSnapshot>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/information`,
      { headers: workspaceHeader(workspaceId) },
    ),
  );
}

/**
 * 读取 Session 内的 durable child thread 列表。
 * 404（Session 不存在）与 409（目录树异常）由 requestJson 透明抛出。
 */
export async function listChildThreads(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<ChildThreadList> {
  return parseChildThreadList(
    unwrapApiData(
      await requestJson<APIResponse<unknown>>(
        port,
        `/api/v1/sessions/${encodeURIComponent(sessionId)}/child-threads`,
        { headers: workspaceHeader(workspaceId) },
      ),
    ),
  );
}

export async function createSession(
  port: number,
  title: string = DEFAULT_SESSION_TITLE,
  workspaceId?: string | null,
  folderId?: string | null,
): Promise<Session> {
  return unwrapApiData(
    await requestJson<APIResponse<Session>>(port, "/api/v1/sessions", {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ title, folder_id: folderId ?? null }),
    }),
  );
}

export async function forkSessionContext(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<Session> {
  return unwrapApiData(
    await requestJson<APIResponse<Session>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/fork-context`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
      },
    ),
  );
}

export async function updateSession(
  port: number,
  sessionId: string,
  payload: SessionUpdateRequest,
  workspaceId?: string | null,
): Promise<Session> {
  return unwrapApiData(
    await requestJson<APIResponse<Session>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "PATCH",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function deleteSession(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
  cascade = false,
): Promise<DeleteSessionResult> {
  return unwrapApiData(
    await requestJson<APIResponse<DeleteSessionResult>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}${cascade ? "?cascade=true" : ""}`,
      { method: "DELETE", headers: workspaceHeader(workspaceId) },
    ),
  );
}

export function updateSessionAgent(
  port: number,
  sessionId: string,
  agentId: string,
  workspaceId?: string | null,
): Promise<Session> {
  return updateSession(port, sessionId, { agent_id: agentId }, workspaceId);
}

export function updateSessionProvider(
  port: number,
  sessionId: string,
  providerId: string,
  workspaceId?: string | null,
): Promise<Session> {
  return updateSession(port, sessionId, { provider_id: providerId }, workspaceId);
}

export async function compactSessionContext(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<SessionCompactResult> {
  return unwrapApiData(
    await requestJson<APIResponse<SessionCompactResult>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/compact`,
      { method: "POST", headers: workspaceHeader(workspaceId) },
    ),
  );
}

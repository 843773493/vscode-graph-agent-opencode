import type {
  APIResponse,
  CursorPage,
  DeleteSessionResult,
  Session,
  SessionCompactResult,
  SessionInformationSnapshot,
  SessionUpdateRequest,
} from "../types/backend";
import { requestJson, unwrapApiData, workspaceHeader } from "./http";

export const DEFAULT_SESSION_TITLE = "新会话";

function normalizePageResult<T>(value: unknown): CursorPage<T> {
  if (!value || typeof value !== "object") {
    return { items: [] };
  }

  const record = value as {
    items?: T[];
    next_cursor?: string | null;
    has_more?: boolean;
  };
  return {
    items: Array.isArray(record.items) ? record.items : [],
    next_cursor: record.next_cursor ?? null,
    has_more:
      typeof record.has_more === "boolean" ? record.has_more : undefined,
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
        }
      : undefined,
  );
  return normalizePageResult<Session>(unwrapApiData(data));
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

import { requestJson, unwrapApiData, workspaceHeader } from "./api";
import type {
  APIResponse,
  PendingRequestList,
  PendingRequestPolicyUpdateRequest,
  PendingRequestUpdateRequest,
} from "./types/backend";


export async function listPendingRequests(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
  signal?: AbortSignal,
): Promise<PendingRequestList> {
  return unwrapApiData(
    await requestJson<APIResponse<PendingRequestList>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/pending-requests`,
      { headers: workspaceHeader(workspaceId), signal },
    ),
  );
}

export async function updatePendingRequest(
  port: number,
  sessionId: string,
  messageId: string,
  payload: PendingRequestUpdateRequest,
  workspaceId?: string | null,
): Promise<PendingRequestList> {
  return unwrapApiData(
    await requestJson<APIResponse<PendingRequestList>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/pending-requests/${encodeURIComponent(messageId)}`,
      {
        method: "PATCH",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function removePendingRequest(
  port: number,
  sessionId: string,
  messageId: string,
  workspaceId?: string | null,
): Promise<PendingRequestList> {
  return unwrapApiData(
    await requestJson<APIResponse<PendingRequestList>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/pending-requests/${encodeURIComponent(messageId)}`,
      { method: "DELETE", headers: workspaceHeader(workspaceId) },
    ),
  );
}

export async function clearPendingRequests(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<PendingRequestList> {
  return unwrapApiData(
    await requestJson<APIResponse<PendingRequestList>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/pending-requests`,
      { method: "DELETE", headers: workspaceHeader(workspaceId) },
    ),
  );
}

export async function updatePendingRequestPolicy(
  port: number,
  sessionId: string,
  messageId: string,
  payload: PendingRequestPolicyUpdateRequest,
  workspaceId?: string | null,
): Promise<PendingRequestList> {
  return unwrapApiData(
    await requestJson<APIResponse<PendingRequestList>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/pending-requests/${encodeURIComponent(messageId)}/policy`,
      {
        method: "PATCH",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  );
}

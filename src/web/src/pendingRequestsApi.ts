import { requestJson, unwrapApiData, workspaceHeader } from "./api";
import type {
  APIResponse,
  PendingRequestList,
  PendingRequestReorderRequest,
  PendingRequestUpdateRequest,
} from "./types/backend";


export async function listPendingRequests(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<PendingRequestList> {
  return unwrapApiData(
    await requestJson<APIResponse<PendingRequestList>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/pending-requests`,
      { headers: workspaceHeader(workspaceId) },
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

export async function reorderPendingRequests(
  port: number,
  sessionId: string,
  payload: PendingRequestReorderRequest,
  workspaceId?: string | null,
): Promise<PendingRequestList> {
  return unwrapApiData(
    await requestJson<APIResponse<PendingRequestList>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/pending-requests/order`,
      {
        method: "PUT",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function sendPendingRequestImmediately(
  port: number,
  sessionId: string,
  messageId: string,
  workspaceId?: string | null,
): Promise<PendingRequestList> {
  return unwrapApiData(
    await requestJson<APIResponse<PendingRequestList>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/pending-requests/${encodeURIComponent(messageId)}/send-immediately`,
      { method: "POST", headers: workspaceHeader(workspaceId) },
    ),
  );
}

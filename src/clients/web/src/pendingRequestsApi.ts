import { requestJson, unwrapApiData, workspaceHeader } from "./api";
import type {
  APIResponse,
  PendingRequestList,
  PendingRequestPolicyUpdateRequest,
} from "./types/backend";


export async function listThreadPendingRequests(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
  signal?: AbortSignal,
  threadId?: string,
): Promise<PendingRequestList> {
  const path = `/api/v1/sessions/${encodeURIComponent(sessionId)}/${threadId === undefined ? "main-thread/pending-requests" : `pending-requests?thread_id=${encodeURIComponent(threadId)}`}`;
  const snapshot = unwrapApiData(
    await requestJson<APIResponse<PendingRequestList>>(
      port,
      path,
      { headers: workspaceHeader(workspaceId), signal },
    ),
  );
  if (threadId !== undefined && (snapshot.session_id !== sessionId || snapshot.thread_id !== threadId)) throw new Error("待处理消息快照的 Session / Thread 归属不一致");
  return snapshot;
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
  threadId?: string,
): Promise<PendingRequestList> {
  return unwrapApiData(
    await requestJson<APIResponse<PendingRequestList>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/pending-requests${threadId === undefined ? "" : `?thread_id=${encodeURIComponent(threadId)}`}`,
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

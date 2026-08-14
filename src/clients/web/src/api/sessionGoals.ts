import type {
  APIResponse,
  SessionGoal,
  SessionGoalUpdateRequest,
} from "../types/backend";
import { requestJson, unwrapApiData, workspaceHeader } from "./http";

export async function getSessionGoal(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<SessionGoal | null> {
  const response = await requestJson<APIResponse<SessionGoal | null>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/goal`,
    { headers: workspaceHeader(workspaceId) },
  );
  if (typeof response.request_id !== "string" || !response.request_id) {
    throw new Error("后端响应缺少 request_id");
  }
  if (!Object.prototype.hasOwnProperty.call(response, "data")) {
    throw new Error("后端响应缺少 data 字段");
  }
  return response.data;
}

export async function updateSessionGoal(
  port: number,
  sessionId: string,
  payload: SessionGoalUpdateRequest,
  workspaceId?: string | null,
): Promise<SessionGoal> {
  return unwrapApiData(
    await requestJson<APIResponse<SessionGoal>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/goal`,
      {
        method: "PUT",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function clearSessionGoal(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<void> {
  await requestJson<APIResponse<unknown>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/goal`,
    { method: "DELETE", headers: workspaceHeader(workspaceId) },
  );
}

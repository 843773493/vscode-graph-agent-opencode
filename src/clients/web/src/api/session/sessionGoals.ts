import type {
  APIResponse,
  SessionGoal,
  SessionGoalUpdateRequest,
} from "../../types/backend";
import {
  requestJson,
  unwrapApiData,
  unwrapApiDataOrNull,
  workspaceHeader,
} from "../http";

export async function getSessionGoal(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<SessionGoal | null> {
  return unwrapApiDataOrNull(
    await requestJson<APIResponse<SessionGoal | null>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/goal`,
      { headers: workspaceHeader(workspaceId) },
    ),
  );
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

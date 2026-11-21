import type {
  APIResponse,
  SessionGoalOperationResultDTO,
  SessionGoalWriteRequest,
} from "../../types/backend";
import {
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "../http";
import { parseSessionGoalOperation } from "../../protocol/sessionGoal";

export async function updateSessionGoal(
  port: number,
  sessionId: string,
  payload: SessionGoalWriteRequest,
  workspaceId?: string | null,
): Promise<SessionGoalOperationResultDTO> {
  const result = parseSessionGoalOperation(unwrapApiData(
    await requestJson<APIResponse<SessionGoalOperationResultDTO>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/goal`,
      {
        method: "PUT",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  ), sessionId, payload.operation_id);
  if (result.status !== "applied" || !result.goal) throw new Error("Goal 设置回执缺少已应用的目标");
  return result;
}

export async function getSessionGoalOperation(
  port: number,
  sessionId: string,
  operationId: string,
  workspaceId?: string | null,
): Promise<SessionGoalOperationResultDTO> {
  return parseSessionGoalOperation(unwrapApiData(await requestJson<APIResponse<SessionGoalOperationResultDTO>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/goal-operations/${encodeURIComponent(operationId)}`,
    { headers: workspaceHeader(workspaceId) },
  )), sessionId, operationId);
}

export async function clearSessionGoal(
  port: number,
  sessionId: string,
  operation: { operation_id: string; expected_control_revision: number },
  workspaceId?: string | null,
): Promise<SessionGoalOperationResultDTO> {
  const result = parseSessionGoalOperation(unwrapApiData(await requestJson<APIResponse<SessionGoalOperationResultDTO>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/goal`,
    { method: "DELETE", headers: workspaceHeader(workspaceId), body: JSON.stringify(operation) },
  )), sessionId, operation.operation_id);
  if (result.status === "unknown" || result.goal != null) throw new Error("Goal 清除回执不是权威缺失状态");
  return result;
}

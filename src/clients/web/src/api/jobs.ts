import type {
  APIResponse,
  Job,
  ToolApproval,
  ToolApprovalSubmitRequest,
  ToolApprovalCommand,
} from "../types/backend";
import { requestJson, unwrapApiData, workspaceHeader } from "./http";

const AGENT_STATE_TIMEOUT_MS = 10000;

export async function getThreadToolApprovals(
  port: number, workspaceId: string | null | undefined, sessionId: string, threadId: string, signal?: AbortSignal,
): Promise<ToolApproval[]> {
  const query = new URLSearchParams({ session_id: sessionId, thread_id: threadId });
  return unwrapApiData(await requestJson<APIResponse<ToolApproval[]>>(port, `/api/v1/jobs/tool-approvals?${query}`, {
    headers: workspaceHeader(workspaceId), signal, timeoutMs: AGENT_STATE_TIMEOUT_MS,
  }));
}

export async function submitToolApprovals(
  port: number, workspaceId: string | null | undefined, jobId: string, payload: ToolApprovalSubmitRequest, signal?: AbortSignal,
): Promise<ToolApprovalCommand> {
  return unwrapApiData(await requestJson<APIResponse<ToolApprovalCommand>>(port, `/api/v1/jobs/${encodeURIComponent(jobId)}/approvals`, {
    method: "POST", headers: workspaceHeader(workspaceId), body: JSON.stringify(payload), signal,
    timeoutMs: AGENT_STATE_TIMEOUT_MS,
  }));
}

export async function getJob(
  port: number,
  jobId: string,
  workspaceId?: string | null,
): Promise<Job> {
  return unwrapApiData(
    await requestJson<APIResponse<Job>>(
      port,
      `/api/v1/jobs/${encodeURIComponent(jobId)}`,
      {
        headers: workspaceHeader(workspaceId),
        timeoutMs: AGENT_STATE_TIMEOUT_MS,
      },
    ),
  );
}

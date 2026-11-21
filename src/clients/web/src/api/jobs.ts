import type {
  APIResponse,
  Job,
  ToolApproval,
  ToolApprovalSubmitRequest,
  ToolApprovalCommand,
  ThreadExecutionControlDTO,
} from "../types/backend";
import { requestJson, unwrapApiData, workspaceHeader } from "./http";

const AGENT_STATE_TIMEOUT_MS = 10000;

export type ExecutionControlAction = "pause" | "resume" | "interrupt" | "stop";

export async function getThreadExecutionControl(
  port: number, workspaceId: string, sessionId: string, threadId: string, signal?: AbortSignal,
): Promise<ThreadExecutionControlDTO> {
  return requestThreadExecutionControl(port, workspaceId, sessionId, threadId, { signal });
}

export async function controlThreadExecution(
  port: number, workspaceId: string, snapshot: ThreadExecutionControlDTO,
  action: ExecutionControlAction, signal?: AbortSignal,
): Promise<ThreadExecutionControlDTO> {
  return requestThreadExecutionControl(port, workspaceId, snapshot.session_id, snapshot.thread_id, {
    method: "POST", signal,
    body: JSON.stringify({ action, expected_execution_id: snapshot.execution_id }),
  });
}

async function requestThreadExecutionControl(
  port: number, workspaceId: string, sessionId: string, threadId: string,
  options: { method?: string; body?: string; signal?: AbortSignal },
): Promise<ThreadExecutionControlDTO> {
  const query = new URLSearchParams({ session_id: sessionId, thread_id: threadId });
  const result = unwrapApiData(await requestJson<APIResponse<ThreadExecutionControlDTO>>(
    port, `/api/v1/jobs/control?${query}`, {
      ...options, headers: workspaceHeader(workspaceId), timeoutMs: AGENT_STATE_TIMEOUT_MS,
    },
  ));
  if (result.session_id !== sessionId || result.thread_id !== threadId) {
    throw new Error(`执行控制响应归属不匹配: ${sessionId}/${threadId}`);
  }
  return result;
}

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

import type {
  APIResponse,
  Job,
  JobControlRequest,
  JobControlResponse,
} from "../types/backend";
import { requestJson, unwrapApiData, workspaceHeader } from "./http";

const AGENT_STATE_TIMEOUT_MS = 10000;

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

export async function controlJob(
  port: number,
  jobId: string,
  payload: JobControlRequest,
  workspaceId?: string | null,
): Promise<JobControlResponse> {
  return unwrapApiData(
    await requestJson<APIResponse<JobControlResponse>>(
      port,
      `/api/v1/jobs/${encodeURIComponent(jobId)}/control`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
        timeoutMs: AGENT_STATE_TIMEOUT_MS,
      },
    ),
  );
}

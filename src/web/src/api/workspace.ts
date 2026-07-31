import type { Agent, APIResponse, WorkspaceInfo } from "../types/backend";
import { requestJson, unwrapApiData, workspaceHeader } from "./http";

export async function getWorkspace(
  port: number,
  workspaceId?: string | null,
): Promise<WorkspaceInfo> {
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceInfo>>(
      port,
      "/api/v1/workspace",
      workspaceId ? { headers: workspaceHeader(workspaceId) } : undefined,
    ),
  );
}

export async function listAgents(
  port: number,
  workspaceId?: string | null,
): Promise<Agent[]> {
  return unwrapApiData(
    await requestJson<APIResponse<Agent[]>>(
      port,
      "/api/v1/agents",
      workspaceId ? { headers: workspaceHeader(workspaceId) } : undefined,
    ),
  );
}

export async function setWorkspaceDefaultAgent(
  port: number,
  agentId: string,
  workspaceId: string,
): Promise<Agent[]> {
  return unwrapApiData(
    await requestJson<APIResponse<Agent[]>>(
      port,
      "/api/v1/agents/workspace-default",
      {
        method: "PUT",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify({ agent_id: agentId }),
      },
    ),
  );
}

export async function setWorkspaceDefaultProvider(
  port: number,
  agentId: string,
  providerId: string,
  workspaceId: string,
): Promise<Agent[]> {
  return unwrapApiData(
    await requestJson<APIResponse<Agent[]>>(
      port,
      `/api/v1/agents/${encodeURIComponent(agentId)}/workspace-default-provider`,
      {
        method: "PUT",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify({ provider_id: providerId }),
      },
    ),
  );
}

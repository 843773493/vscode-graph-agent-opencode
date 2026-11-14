import type { Agent, APIResponse, WorkspaceInfo } from "../types/backend";
import type { GatewayWorkspaceRequestTarget } from "./workspaceIdentity";
import {
  gatewayWorkspaceRequestHeaders,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "./http";

type AgentWorkspaceTarget = string | GatewayWorkspaceRequestTarget;
const agentHeaders = (target?: AgentWorkspaceTarget | null) => typeof target === "object" && target !== null
  ? gatewayWorkspaceRequestHeaders(target) : workspaceHeader(target);

export async function getWorkspace(
  port: number,
  workspaceId?: string | null,
  signal?: AbortSignal,
): Promise<WorkspaceInfo> {
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceInfo>>(
      port,
      "/api/v1/workspace",
      { headers: workspaceHeader(workspaceId), signal },
    ),
  );
}

export async function listAgents(
  port: number,
  workspaceId?: AgentWorkspaceTarget | null,
  signal?: AbortSignal,
): Promise<Agent[]> {
  return unwrapApiData(
    await requestJson<APIResponse<Agent[]>>(
      port,
      "/api/v1/agents",
      { headers: agentHeaders(workspaceId), signal },
    ),
  );
}

export async function setWorkspaceDefaultAgent(
  port: number,
  agentId: string,
  workspaceId: AgentWorkspaceTarget,
): Promise<Agent[]> {
  return unwrapApiData(
    await requestJson<APIResponse<Agent[]>>(
      port,
      "/api/v1/agents/workspace-default",
      {
        method: "PUT",
        headers: agentHeaders(workspaceId),
        body: JSON.stringify({ agent_id: agentId }),
      },
    ),
  );
}

export async function setWorkspaceDefaultProvider(
  port: number,
  agentId: string,
  providerId: string,
  workspaceId: AgentWorkspaceTarget,
): Promise<Agent[]> {
  return unwrapApiData(
    await requestJson<APIResponse<Agent[]>>(
      port,
      `/api/v1/agents/${encodeURIComponent(agentId)}/workspace-default-provider`,
      {
        method: "PUT",
        headers: agentHeaders(workspaceId),
        body: JSON.stringify({ provider_id: providerId }),
      },
    ),
  );
}

import type { APIResponse } from "../types/backend";
import type {
  ToolCatalogItem,
  ToolSelectionChange,
  ToolTestRun,
  ToolTestRunList,
} from "../types/toolTesting";
import { requestJson, unwrapApiData, workspaceHeader } from "./http";

export async function getToolCatalog(
  port: number,
  agentId: string,
  workspaceId?: string | null,
): Promise<ToolCatalogItem[]> {
  const query = new URLSearchParams({ agent_id: agentId });
  return unwrapApiData(await requestJson<APIResponse<ToolCatalogItem[]>>(
    port,
    `/api/v1/tools?${query.toString()}`,
    { headers: workspaceHeader(workspaceId) },
  ));
}

export async function updateToolSelection(
  port: number,
  agentId: string,
  changes: ToolSelectionChange[],
  workspaceId?: string | null,
): Promise<ToolCatalogItem[]> {
  return unwrapApiData(await requestJson<APIResponse<ToolCatalogItem[]>>(
    port,
    "/api/v1/tools/selection",
    {
      method: "PATCH",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ agent_id: agentId, changes }),
    },
  ));
}

export async function startToolTest(
  port: number,
  toolId: string,
  agentId: string,
  workspaceId?: string | null,
): Promise<ToolTestRun> {
  return unwrapApiData(await requestJson<APIResponse<ToolTestRun>>(
    port,
    `/api/v1/tools/${encodeURIComponent(toolId)}/tests`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ agent_id: agentId, provider_ids: [] }),
    },
  ));
}

export async function getToolTestRun(
  port: number,
  runId: string,
  workspaceId?: string | null,
): Promise<ToolTestRun> {
  return unwrapApiData(await requestJson<APIResponse<ToolTestRun>>(
    port,
    `/api/v1/tools/tests/${encodeURIComponent(runId)}`,
    { headers: workspaceHeader(workspaceId) },
  ));
}

export async function listToolTestRuns(
  port: number,
  workspaceId?: string | null,
): Promise<ToolTestRun[]> {
  return unwrapApiData(await requestJson<APIResponse<ToolTestRunList>>(
    port,
    "/api/v1/tools/tests?limit=50",
    { headers: workspaceHeader(workspaceId) },
  )).items;
}

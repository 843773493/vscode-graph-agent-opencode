import type { APIResponse } from "../types/backend";
import type { ResourceThreadRef, WorkspaceResourceDTO, WorkspaceResourceListDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import { requestJson, unwrapApiData, workspaceHeader } from "./http";

export type { ResourceThreadRef, WorkspaceResourceDTO };
export type ResourceAction = "promote" | "attach" | "detach" | "stop" | "delete";

export async function listWorkspaceTerminals(port: number, workspaceId: string): Promise<WorkspaceResourceListDTO> {
  return unwrapApiData(await requestJson<APIResponse<WorkspaceResourceListDTO>>(
    port, "/api/v1/resources?kind=terminal", { headers: workspaceHeader(workspaceId) },
  ));
}

export async function getWorkspaceResource(port: number, workspaceId: string, resourceId: string): Promise<WorkspaceResourceDTO> {
  return unwrapApiData(await requestJson<APIResponse<WorkspaceResourceDTO>>(
    port, `/api/v1/resources/${encodeURIComponent(resourceId)}`, { headers: workspaceHeader(workspaceId) },
  ));
}

export async function createWorkspaceTerminal(
  port: number, workspaceId: string, thread: ResourceThreadRef,
  retentionScope: "thread" | "workspace" = "thread",
): Promise<WorkspaceResourceDTO> {
  return unwrapApiData(await requestJson<APIResponse<WorkspaceResourceDTO>>(
    port, "/api/v1/resources/terminals", {
      method: "POST", headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ session_id: thread.session_id, thread_id: thread.thread_id, retention_scope: retentionScope, title: "用户终端" }),
    },
  ));
}

export async function createWorkspaceBrowser(
  port: number, workspaceId: string, thread: ResourceThreadRef,
  retentionScope: "thread" | "workspace" = "thread",
): Promise<WorkspaceResourceDTO> {
  return unwrapApiData(await requestJson<APIResponse<WorkspaceResourceDTO>>(
    port, "/api/v1/resources/browsers", {
      method: "POST", headers: workspaceHeader(workspaceId),
      body: JSON.stringify({
        session_id: thread.session_id, thread_id: thread.thread_id, retention_scope: retentionScope,
        title: "用户浏览器", url: "about:blank", viewport: { width: 1280, height: 800 },
      }),
    },
  ));
}

export async function controlWorkspaceResource(
  port: number, workspaceId: string, resource: WorkspaceResourceDTO,
  action: ResourceAction, thread?: ResourceThreadRef,
): Promise<WorkspaceResourceDTO> {
  if (action === "attach" && !thread) throw new Error("关联资源必须提供明确的 session_id 与 thread_id");
  return unwrapApiData(await requestJson<APIResponse<WorkspaceResourceDTO>>(
    port, `/api/v1/resources/${encodeURIComponent(resource.resource_id)}/${action}`, {
      method: "POST", headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ expected_revision: resource.owner_revision, thread }),
    },
  ));
}

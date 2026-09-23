import type {
  APIResponse,
  Session,
  SessionCatalogNode,
  SessionCatalogPage,
} from "../../types/backend";
import { requestJson, unwrapApiData, workspaceHeader } from "../http";

/**
 * 会话目录节点数组的唯一合法性校验：node_id 必须是非空字符串且在同一数组内唯一。
 * node_id 同时充当 React key、展开状态键与分支键，缺一个就会让文件夹展开塌缩到
 * 根分支并无限递归，重复则让 React 静默复用或丢弃整行。两者都是契约被破坏，
 * 必须在此响亮失败，由既有分支错误通路展示，不能留给 React 警告。
 */
function assertSessionCatalogNodes(
  items: SessionCatalogNode[],
  context: string,
): void {
  const seen = new Set<string>();
  for (const [index, node] of items.entries()) {
    const nodeId = (node as { node_id?: unknown }).node_id;
    if (typeof nodeId !== "string" || nodeId === "") {
      throw new Error(
        `${context}第 ${index + 1} 个节点的 node_id 必须是非空字符串，实际为 ${JSON.stringify(nodeId)}`,
      );
    }
    if (seen.has(nodeId)) {
      throw new Error(`${context}存在重复的 node_id: ${nodeId}`);
    }
    seen.add(nodeId);
  }
}

export async function listSessionCatalogChildren(
  port: number,
  workspaceId: string,
  parentNodeId?: string | null,
  cursor?: string | null,
): Promise<SessionCatalogPage> {
  const query = new URLSearchParams({ limit: "100" });
  if (parentNodeId) query.set("parent_node_id", parentNodeId);
  if (cursor) query.set("cursor", cursor);
  const page = unwrapApiData(await requestJson<APIResponse<SessionCatalogPage>>(
    port,
    `/api/v1/session-catalog/children?${query.toString()}`,
    { headers: workspaceHeader(workspaceId) },
  ));
  assertSessionCatalogNodes(page.items, "会话目录子节点");
  return page;
}

export async function refreshSessionCatalog(
  port: number,
  workspaceId: string,
): Promise<SessionCatalogPage> {
  const page = unwrapApiData(await requestJson<APIResponse<SessionCatalogPage>>(
    port,
    "/api/v1/session-catalog/refresh",
    { method: "POST", headers: workspaceHeader(workspaceId) },
  ));
  assertSessionCatalogNodes(page.items, "会话目录刷新");
  return page;
}

export async function createSessionCatalogFolder(
  port: number,
  workspaceId: string,
  name: string,
  parentFolderId?: string | null,
): Promise<void> {
  await requestJson<APIResponse<unknown>>(port, "/api/v1/session-catalog/folders", {
    method: "POST",
    headers: workspaceHeader(workspaceId),
    body: JSON.stringify({ name, parent_folder_id: parentFolderId ?? null }),
  });
}

async function patchCatalogFolder(
  port: number,
  workspaceId: string,
  folderId: string,
  body: Record<string, unknown>,
): Promise<void> {
  await requestJson<APIResponse<unknown>>(
    port,
    `/api/v1/session-catalog/folders/${encodeURIComponent(folderId)}`,
    { method: "PATCH", headers: workspaceHeader(workspaceId), body: JSON.stringify(body) },
  );
}

export function renameSessionCatalogFolder(
  port: number,
  workspaceId: string,
  folderId: string,
  name: string,
): Promise<void> {
  return patchCatalogFolder(port, workspaceId, folderId, { name });
}

export function moveSessionCatalogFolder(
  port: number,
  workspaceId: string,
  folderId: string,
  parentFolderId?: string | null,
): Promise<void> {
  return patchCatalogFolder(port, workspaceId, folderId, {
    parent_folder_id: parentFolderId ?? null,
  });
}

export async function deleteSessionCatalogFolder(
  port: number,
  workspaceId: string,
  folderId: string,
): Promise<void> {
  await requestJson<unknown>(
    port,
    `/api/v1/session-catalog/folders/${encodeURIComponent(folderId)}?recursive=true`,
    { method: "DELETE", headers: workspaceHeader(workspaceId) },
  );
}

export async function assignSessionCatalogFolder(
  port: number,
  workspaceId: string,
  sessionId: string,
  folderId?: string | null,
): Promise<void> {
  await requestJson<APIResponse<unknown>>(
    port,
    `/api/v1/session-catalog/sessions/${encodeURIComponent(sessionId)}/folder`,
    {
      method: "PUT",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ folder_id: folderId ?? null }),
    },
  );
}

export async function moveSessionCatalogNode(
  port: number,
  workspaceId: string,
  nodeId: string,
  parentNodeId: string | null,
): Promise<void> {
  await requestJson<APIResponse<unknown>>(
    port,
    `/api/v1/session-catalog/nodes/${encodeURIComponent(nodeId)}/parent`,
    {
      method: "PATCH",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ parent_node_id: parentNodeId }),
    },
  );
}

export async function moveSessionParent(
  port: number,
  workspaceId: string,
  sessionId: string,
  parentNodeId: string | null,
): Promise<Session> {
  await moveSessionCatalogNode(port, workspaceId, sessionId, parentNodeId);
  return unwrapApiData(await requestJson<APIResponse<Session>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    { headers: workspaceHeader(workspaceId) },
  ));
}

export async function getSessionCatalogBreadcrumb(
  port: number,
  workspaceId: string,
  nodeId: string,
): Promise<{ items: SessionCatalogNode[] }> {
  const breadcrumb = unwrapApiData(await requestJson<APIResponse<{ items: SessionCatalogNode[] }>>(
    port,
    `/api/v1/session-catalog/breadcrumb/${encodeURIComponent(nodeId)}`,
    { headers: workspaceHeader(workspaceId) },
  ));
  assertSessionCatalogNodes(breadcrumb.items, "会话目录面包屑");
  return breadcrumb;
}

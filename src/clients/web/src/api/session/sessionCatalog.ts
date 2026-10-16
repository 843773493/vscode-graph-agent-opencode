import type {
  APIResponse,
  SessionCatalogBreadcrumb,
  SessionCatalogExport,
  SessionCatalogNode,
  SessionCatalogPage,
} from "../../types/backend";
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData } from "../http";
import type { GatewayWorkspaceRequestTarget } from "../workspaceIdentity";

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
  workspaceTarget: GatewayWorkspaceRequestTarget,
  parentNodeId?: string | null,
  cursor?: string | null,
  options: { signal?: AbortSignal } = {},
): Promise<SessionCatalogPage> {
  const query = new URLSearchParams({ limit: "100" });
  if (parentNodeId) query.set("parent_node_id", parentNodeId);
  if (cursor) query.set("cursor", cursor);
  const page = unwrapApiData(await requestJson<APIResponse<SessionCatalogPage>>(
    port,
    `/api/v1/session-catalog/children?${query.toString()}`,
    { headers: gatewayWorkspaceRequestHeaders(workspaceTarget), signal: options.signal },
  ));
  assertSessionCatalogNodes(page.items, "会话目录子节点");
  return page;
}

export async function refreshSessionCatalog(
  port: number,
  workspaceTarget: GatewayWorkspaceRequestTarget,
  options: { signal?: AbortSignal } = {},
): Promise<SessionCatalogPage> {
  const page = unwrapApiData(await requestJson<APIResponse<SessionCatalogPage>>(
    port,
    "/api/v1/session-catalog/refresh",
    { method: "POST", headers: gatewayWorkspaceRequestHeaders(workspaceTarget), signal: options.signal },
  ));
  assertSessionCatalogNodes(page.items, "会话目录刷新");
  return page;
}

/** 完整的可见节点集用于确认已加载分支是否仍可读取，不从单个分页推断节点消失。 */
export async function getSessionCatalogExport(
  port: number,
  workspaceTarget: GatewayWorkspaceRequestTarget,
  options: { signal?: AbortSignal } = {},
): Promise<SessionCatalogExport> {
  const catalog = unwrapApiData(await requestJson<APIResponse<SessionCatalogExport>>(
    port,
    "/api/v1/session-catalog/export",
    { headers: gatewayWorkspaceRequestHeaders(workspaceTarget), signal: options.signal },
  ));
  assertSessionCatalogNodes(catalog.items, "会话目录完整节点集");
  return catalog;
}

export async function getSessionCatalogBreadcrumb(
  port: number,
  workspaceTarget: GatewayWorkspaceRequestTarget,
  nodeId: string,
): Promise<SessionCatalogBreadcrumb> {
  const breadcrumb = unwrapApiData(await requestJson<APIResponse<SessionCatalogBreadcrumb>>(
    port,
    `/api/v1/session-catalog/breadcrumb/${encodeURIComponent(nodeId)}`,
    { headers: gatewayWorkspaceRequestHeaders(workspaceTarget) },
  ));
  assertSessionCatalogNodes(breadcrumb.items, "会话目录面包屑");
  return breadcrumb;
}

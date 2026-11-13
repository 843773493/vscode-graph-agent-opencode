import type {
  APIResponse,
  SessionCatalogBreadcrumb,
  SessionCatalogExport,
  SessionCatalogNode,
  SessionCatalogPage,
} from "../../types/backend";
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData } from "../http";
import type { GatewayWorkspaceRequestTarget } from "../workspaceIdentity";

type CatalogReadOptions = { signal?: AbortSignal; minimumCatalogRevision?: number; knownRevision?: string };

function readHeaders(target: GatewayWorkspaceRequestTarget, options: CatalogReadOptions): Record<string, string> {
  return {
    ...gatewayWorkspaceRequestHeaders(target),
    ...(options.knownRevision ? { "X-BoxTeam-Catalog-Revision": options.knownRevision } : {}),
  };
}

function readQuery(options: CatalogReadOptions): URLSearchParams {
  const query = new URLSearchParams();
  if (options.minimumCatalogRevision !== undefined) {
    if (!Number.isSafeInteger(options.minimumCatalogRevision) || options.minimumCatalogRevision < 0) {
      throw new Error("目录读取下限必须是非负安全整数");
    }
    query.set("minimum_catalog_revision", String(options.minimumCatalogRevision));
  }
  return query;
}

function assertCatalogScope(
  value: Pick<SessionCatalogPage, "revision" | "catalog_revision" | "sync">,
  target: GatewayWorkspaceRequestTarget,
  options: CatalogReadOptions,
): void {
  if (!value.sync || value.sync.gateway_id !== target.gateway_id || value.sync.workspace_id !== target.workspace_id) {
    throw new Error("会话目录响应 Gateway／工作区归属不一致");
  }
  if (!Number.isSafeInteger(value.catalog_revision) || value.catalog_revision < (options.minimumCatalogRevision ?? 0)) {
    throw new Error(`会话目录 catalog revision 落后: ${value.catalog_revision} < ${options.minimumCatalogRevision ?? 0}`);
  }
  if (value.sync.source === "gateway_mirror" && options.knownRevision !== undefined && value.revision !== options.knownRevision) {
    throw new Error("会话目录镜像尚未确认已知投影版本");
  }
}

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
  options: CatalogReadOptions = {},
): Promise<SessionCatalogPage> {
  const query = readQuery(options);
  query.set("limit", "100");
  if (parentNodeId) query.set("parent_node_id", parentNodeId);
  if (cursor) query.set("cursor", cursor);
  const page = unwrapApiData(await requestJson<APIResponse<SessionCatalogPage>>(
    port,
    `/api/v1/session-catalog/children?${query.toString()}`,
    { headers: readHeaders(workspaceTarget, options), signal: options.signal },
  ));
  assertSessionCatalogNodes(page.items, "会话目录子节点");
  assertCatalogScope(page, workspaceTarget, options);
  if ((page.parent_node_id ?? null) !== (parentNodeId ?? null)) throw new Error("会话目录响应父节点作用域不一致");
  return page;
}

export async function refreshSessionCatalog(
  port: number,
  workspaceTarget: GatewayWorkspaceRequestTarget,
  options: CatalogReadOptions = {},
): Promise<SessionCatalogPage> {
  const query = readQuery(options);
  const page = unwrapApiData(await requestJson<APIResponse<SessionCatalogPage>>(
    port,
    `/api/v1/session-catalog/refresh${query.size ? `?${query}` : ""}`,
    { method: "POST", headers: readHeaders(workspaceTarget, options), signal: options.signal },
  ));
  assertSessionCatalogNodes(page.items, "会话目录刷新");
  assertCatalogScope(page, workspaceTarget, options);
  return page;
}

/** 按权威快照确认指定节点是否可见；不从单个分页推断节点消失。 */
export async function getSessionCatalogExport(
  port: number,
  workspaceTarget: GatewayWorkspaceRequestTarget,
  options: CatalogReadOptions & { nodeIds: readonly string[] },
): Promise<SessionCatalogExport> {
  const query = readQuery(options);
  for (const nodeId of options.nodeIds) query.append("node_id", nodeId);
  const catalog = unwrapApiData(await requestJson<APIResponse<SessionCatalogExport>>(
    port,
    `/api/v1/session-catalog/export?${query.toString()}`,
    { headers: readHeaders(workspaceTarget, options), signal: options.signal },
  ));
  assertSessionCatalogNodes(catalog.items, "会话目录节点确认");
  assertCatalogScope(catalog, workspaceTarget, options);
  const requested = [...new Set(options.nodeIds)].sort();
  if (catalog.complete || catalog.node_ids.length !== requested.length
    || [...catalog.node_ids].sort().some((nodeId, index) => nodeId !== requested[index])
    || catalog.items.some(node => !requested.includes(node.node_id))) {
    throw new Error("会话目录节点确认响应范围不一致");
  }
  return catalog;
}

export async function getSessionCatalogBreadcrumb(
  port: number,
  workspaceTarget: GatewayWorkspaceRequestTarget,
  nodeId: string,
  options: CatalogReadOptions = {},
): Promise<SessionCatalogBreadcrumb> {
  const query = readQuery(options);
  const breadcrumb = unwrapApiData(await requestJson<APIResponse<SessionCatalogBreadcrumb>>(
    port,
    `/api/v1/session-catalog/breadcrumb/${encodeURIComponent(nodeId)}${query.size ? `?${query}` : ""}`,
    { headers: readHeaders(workspaceTarget, options), signal: options.signal },
  ));
  assertSessionCatalogNodes(breadcrumb.items, "会话目录面包屑");
  assertCatalogScope(breadcrumb, workspaceTarget, options);
  if (breadcrumb.node_id !== nodeId) throw new Error("会话目录面包屑响应节点作用域不一致");
  return breadcrumb;
}

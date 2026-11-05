import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey } from "../workspaceIdentity";
import type { APIResponse, WorkspaceNavigationTree } from "../../types/backend";
import type { GatewaySearchScopeResolutionDTO, GatewaySearchWorkspaceOwnerDTO, GatewayWorkspaceDiscoveryResultsDTO } from "../../types/protocol_generated/boxteam/gateway/v2/public";
import type { FederatedSearchSourceDTO } from "../../types/protocol_generated/boxteam/search/v1/public";
import { requestJson, unwrapApiData } from "../http";
import { searchBranchKey, streamFederatedSearch } from "../stream/federatedSearch";

/** 目录界面的展示投影；领域正文和来源均取自原联邦页。 */
export interface CatalogSearchItem {
  workspace_id: string;
  workspace_name: string;
  owner?: GatewaySearchWorkspaceOwnerDTO;
  node_id: string;
  node_kind: "workspace_folder" | "workspace" | "folder" | "session";
  name: string;
  session_id?: string;
  relative_path: string;
  breadcrumb_node_ids: string[];
}

export interface CatalogSearchView {
  items: CatalogSearchItem[];
  workspaces: { workspace_id: string; workspace_name: string; status: string; error?: string;
    source?: FederatedSearchSourceDTO; limited: boolean }[];
  limited: boolean;
}

export async function resolveCatalogSearchTarget(port: number, owner: GatewaySearchWorkspaceOwnerDTO): Promise<string> {
  const scope = unwrapApiData(await requestJson<APIResponse<GatewaySearchScopeResolutionDTO>>(port, "/api/gateway/search/resolve", {
    method: "POST", body: JSON.stringify({ mode: "origin", origin: owner }),
  }));
  const branch = scope.branches[0];
  if (!branch?.owner || branch.state !== "registered") throw new Error("原目录结果的 owner 已无可用登记工作区");
  if (branch.owner.gateway_id !== owner.gateway_id || branch.owner.workspace_id !== owner.workspace_id) throw new Error("目录解析返回了其它 owner");
  return gatewayWorkspaceKey(branch.owner);
}

export async function searchCatalogView(port: number, query: string, navigation: WorkspaceNavigationTree | null,
  signal: AbortSignal, publish: (view: CatalogSearchView) => void,
): Promise<void> {
  const controller = new AbortController();
  const querySignal = AbortSignal.any([signal, controller.signal]);
  const labels = new Map(navigation?.nodes.filter(node => node.kind === "workspace_ref").map(node => [nullableGatewayWorkspaceKey(node.workspace), node.name]));
  const pages = new Map<string, CatalogSearchItem[]>();
  const branches = new Map<string, CatalogSearchView["workspaces"][number]>();
  let controls: CatalogSearchItem[] = [], controlLimited = false;
  const update = () => {
    if (!querySignal.aborted) publish({ items: [...controls, ...pages.values()].flat(), workspaces: [...branches.values()],
      limited: controlLimited || [...branches.values()].some(branch => branch.limited) });
  };
  // 控制面发现只读登记和导航；业务目录只发送一次明确 all 的联邦请求。
  const tasks = [
    requestJson<APIResponse<GatewayWorkspaceDiscoveryResultsDTO>>(port,
      `/api/gateway/search/workspaces?${new URLSearchParams({ query, include_folders: "true", limit: "200" })}`, { signal: querySignal },
    ).then(response => {
      const discovery = unwrapApiData(response);
      controls = [
        ...discovery.items.map(item => ({ workspace_id: gatewayWorkspaceKey(item.owner!), workspace_name: item.name, owner: item.owner,
          node_id: item.navigation_node_id, node_kind: "workspace" as const, name: item.name,
          relative_path: item.breadcrumb_names.join("/"), breadcrumb_node_ids: item.breadcrumb_node_ids })),
        ...discovery.folders.map(item => ({ workspace_id: "gateway-navigation", workspace_name: "工作区目录",
          node_id: item.navigation_node_id, node_kind: "workspace_folder" as const, name: item.name,
          relative_path: item.breadcrumb_names.join("/"), breadcrumb_node_ids: item.breadcrumb_node_ids })),
      ];
      controlLimited = !discovery.complete;
      update();
    }),
    streamFederatedSearch(port, { scope: { mode: "all", owners: [] }, queries: [{ category: "catalog", operation: "name",
      contract_version: 1, query, match_mode: "fuzzy", limit: 50, file_targets: [], diagnostic_targets: [], allow_catalog_mirror: true }],
      continuations: [], max_results: 200 }, { signal: querySignal, onEvent: event => {
      if (event.type === "scope") {
        for (const branch of event.scope?.branches ?? []) {
          if (!branch.owner) throw new Error("目录范围缺少稳定 owner");
          const route = gatewayWorkspaceKey(branch.owner);
          branches.set(searchBranchKey({ owner: branch.owner, category: "catalog", operation: "name" }), {
            workspace_id: route, workspace_name: labels.get(route) ?? route, status: "searching", limited: false,
          });
        }
      } else if (event.branch?.branch) {
        const key = searchBranchKey(event.branch.branch), branch = branches.get(key);
        if (!branch) throw new Error("目录响应不属于本次范围");
        if (event.type === "page") {
          const catalog = event.page?.catalog;
          if (!catalog) throw new Error("目录响应缺少原 catalog 页");
          pages.set(key, catalog.items.map(item => {
            if (!item.node) throw new Error("目录响应缺少原节点");
            return { workspace_id: branch.workspace_id, workspace_name: branch.workspace_name, owner: event.branch!.branch!.owner,
              node_id: item.node.node_id, node_kind: item.node.kind as "folder" | "session", name: item.node.name,
              session_id: item.node.session_id, relative_path: item.relative_path,
              breadcrumb_node_ids: item.breadcrumb.map(node => node.node_id) };
          }));
          branch.source = event.source;
        }
        if (event.type === "branch") {
          branch.status = event.branch.state === "failed" || event.branch.state === "cancelled" ? "unavailable"
            : branch.source?.stale ? "stale" : "available";
          branch.error = event.branch.message ?? branch.source?.unavailable_reason;
          branch.limited = event.branch.state === "limited";
        }
      }
      update();
    } }),
  ];
  try {
    await Promise.all(tasks);
  } catch (error) {
    controller.abort(error);
    await Promise.allSettled(tasks);
    throw error;
  }
}

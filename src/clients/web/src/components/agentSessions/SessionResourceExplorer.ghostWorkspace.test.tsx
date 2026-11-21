import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { GatewayWorkspace, SessionCatalogNode, SessionCatalogPage } from "../../types/backend";
import { sessionCatalogOutboxScopeKey } from "../../hooks/session/sessionCatalogOutboxRuntime";
import {
  addCatalogOutboxIntent,
  catalogOutboxPartitionKey,
  createCatalogOutbox,
} from "../../state/session/sessionCatalogOutbox";
import {
  apiSuccessResponse,
  catalogReadResponse,
  type GatewayFetchRequest,
  installGatewayFetch,
  restoreGatewayTestEnvironment,

} from "../../tests/fixtures/session/gatewayEnvironment";
import { installGatewayTestWindow } from "../../tests/fixtures/session/gatewayBrowserGlobals";
import { createDeferredResult } from "../../tests/fixtures/session/asyncControl";
import SessionResourceExplorer, { type SessionResourceExplorerActions } from "./SessionResourceExplorer";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { FederatedSearchEventDTO } from "../../types/protocol_generated/boxteam/search/v1/public";
import { setLocaleResources } from "../../../../shared/core/i18n/index.js";
import english from "../../../../shared/core/i18n/messages/en.json";
import chinese from "../../../../shared/core/i18n/messages/zh-cn.json";

const WORKSPACE_ID = "00000000-0000-0000-0000-000000000001";
const OTHER_WORKSPACE_ID = "00000000-0000-0000-0000-000000000002";
const GONE_WORKSPACE_ID = "00000000-0000-0000-0000-000000000003";
const WORKSPACE_KEY = gatewayWorkspaceKey({ gateway_id: "gateway-test", workspace_id: WORKSPACE_ID });

// 用真实 useSessionResourceExplorer + fetch 桩渲染导航树。不再替换 hook 与浮层模块：
// bun 的 mock.module 是进程级且不可撤销的，会污染同一进程后续所有测试文件看到的
// 这两个模块。

afterEach(() => {
  restoreGatewayTestEnvironment();
  setLocaleResources("zh-cn", chinese);
});


/** 带可读 detail 的错误信封：HttpRequestError 从顶层 detail/message 取诊断文本。 */
function apiErrorResponse(detail: string): Response {
  return Response.json(
    { code: 500, message: "ok", request_id: "req_test", detail },
    { status: 500 },
  );
}

function navigationNode(workspaceId: string, name: string) {
  return {
    node_id: `gwn_${workspaceId}`,
    kind: "workspace_ref",
    name,
    parent_node_id: null,
    workspace: { gateway_id: "gateway-test", workspace_id: workspaceId },
    position: 0,
    parent_path: null,
  };
}

function gatewayWorkspace(
  workspaceId: string,
  status: "ready" | "offline",
  connectionError: string | null = null,
): GatewayWorkspace {
  return { gateway_id: "gateway-test", workspace_id: workspaceId, parent_workspace: null, name: workspaceId, backend_url: "http://127.0.0.1:9000", connection_kind: "local", status, active: false, managed: true, affected_workspace_ids: [], removable: true, system_default: false, runtime_action: "safe_restart_managed_backend", remote_gateways: [], services: {}, connection_error: connectionError, checked_at: "2026-07-31T00:00:00Z" };
}

/** 导航树渲染：喂入指定节点与工作区，等 effect 收敛后返回渲染出的 HTML。 */
async function renderExplorer(options: {
  nodes: unknown[];
  workspaces: GatewayWorkspace[];
  navigationMissing?: boolean;
  /** 导航请求一直挂起：用于验证「尚未加载」的加载态。 */
  navigationPending?: boolean;
  navigationFails?: string | null;
  branchItems?: unknown[];
  branchError?: string | null;
  branchResponse?: (parentNodeId: string | null, request: GatewayFetchRequest) => Response | Promise<Response>;
  catalogItems?: SessionCatalogNode[];
  /** 激活工作区与当前会话：用于触发「定位当前会话」链路。 */
  activeWorkspaceId?: string | null;
  currentSessionId?: string;
  breadcrumbFails?: string | null;
  searchDiagnostics?: boolean;
  searchMixedDiagnostics?: boolean;
  searchNotices?: boolean;
  searchHttpStatus?: null;
  searchOpen?: boolean;
  searchQuery?: string;
  onActivateWorkspace?: (workspaceId: string) => Promise<void>;
}): Promise<{ html: string; tree: ReactTestRenderer; diagnostics: Array<{ title: string; detail: string }> }> {
  const diagnostics: Array<{ title: string; detail: string }> = [];
  installGatewayTestWindow(8014);
  installGatewayFetch((request) => {
    if (request.path.includes("/api/gateway/workspace-navigation")) {
      if (options.navigationFails) {
        return apiErrorResponse(options.navigationFails);
      }
      if (options.navigationPending) {
        return new Promise<Response>(() => {});
      }
      if (options.navigationMissing) {
        return undefined;
      }
      return apiSuccessResponse({ revision: "navigation", nodes: options.nodes });
    }
    if (request.path === "/api/gateway/search/workspaces" && options.searchDiagnostics) {
      return apiSuccessResponse({ gateway_id: "gateway-test", query: "needle", resolved_at: "2026-11-12T00:00:00Z",
        source: "registry", items: [], folders: [], complete: true });
    }
    if (request.path === "/api/gateway/search" && options.searchDiagnostics) {
      const owner = { gateway_id: "gateway-test", workspace_id: WORKSPACE_ID };
      const otherOwner = { gateway_id: "gateway-test", workspace_id: OTHER_WORKSPACE_ID };
      const owners = options.searchMixedDiagnostics ? [owner, otherOwner] : [owner];
      const branch = { branch: { owner, category: "catalog", operation: "name" }, state: options.searchMixedDiagnostics ? "complete" : "failed",
        ...(options.searchMixedDiagnostics ? {} : { code: "owner_unavailable", message: "目标已移除", error_owner: "search_transport",
          reason: "route_removed", http_status: 503 }),
        retryable: !options.searchMixedDiagnostics, allocated_results: 50, returned_results: 0,
        ...(options.searchNotices === false ? {} : { diagnostics: [{ severity: "warning", code: "stale_catalog_mirror",
          owner: "gateway_catalog_mirror", message: "[执行](command:workbench.action.danger)" }] }) };
      const completed = { branch: { owner: otherOwner, category: "catalog", operation: "name" }, state: "complete",
        retryable: false, allocated_results: 50, returned_results: 0 };
      const scope = { scope: { version: 1, gateway_id: "gateway-test", resolved_at: "2026-11-12T00:00:00Z", mode: "all", owners },
        branches: owners.map(owner => ({ owner, state: "registered" })) };
      const frames = [
        { type: "scope", scope },
        { type: "branch", branch },
        ...(options.searchMixedDiagnostics ? [{ type: "branch", branch: completed }] : []),
        { type: "end", summary: { state: options.searchMixedDiagnostics ? "complete" : "incomplete", order: "category-scope-owner-v1",
          branches: [branch, ...(options.searchMixedDiagnostics ? [completed] : [])], returned_results: 0 } },
      ].map((payload, index) => {
        const event = FederatedSearchEventDTO.create({ ...payload, sequence: index + 1, request_id: "catalog-diagnostic-request" });
        // 后端 JSON 的可选状态允许 null，不能只验生成类型省略字段的投影。
        const wireEvent = options.searchHttpStatus === null && event.branch
          ? { ...event, branch: { ...event.branch, http_status: null } } : event;
        return `id: ${index + 1}\nevent: search\ndata: ${JSON.stringify(wireEvent)}\n\n`;
      }).join("");
      return new Response(frames, { headers: { "content-type": "text/event-stream", "X-Request-ID": "catalog-diagnostic-request" } });
    }
    if (request.path.includes("/api/v1/session-catalog/breadcrumb/")) {
      if (options.breadcrumbFails) {
        return apiErrorResponse(options.breadcrumbFails);
      }
      return catalogReadResponse(request, { items: [] });
    }
    if (request.path === "/api/v1/session-catalog/export" && options.catalogItems) {
      return catalogReadResponse(request, { revision: "catalog-3", catalog_revision: 3, items: options.catalogItems });
    }
    if (request.path.includes("/api/v1/session-catalog/children")) {
      if (options.branchResponse) {
        return options.branchResponse(new URL(request.url, "http://localhost").searchParams.get("parent_node_id"), request);
      }
      if (options.branchError) {
        return apiErrorResponse(options.branchError);
      }
      return catalogReadResponse(request, {
        revision: "catalog",
        catalog_revision: 3,
        parent_node_id: null,
        items: options.branchItems ?? [],
        cursor: null,
        total: 0,
      });
    }
    return undefined;
  });

  let tree!: ReactTestRenderer;
  let actions: SessionResourceExplorerActions | undefined;
  await act(async () => {
    tree = create(
      <SessionResourceExplorer
        onActionsChange={value => { actions = value; }}
        apiPort={8014}
        workspaces={options.workspaces}
        activeWorkspaceId={options.activeWorkspaceId ? gatewayWorkspaceKey({ gateway_id: "gateway-test", workspace_id: options.activeWorkspaceId }) : null}
        currentSessionId={options.currentSessionId ?? ""}
        selectedSessionKey={null}
        currentSessionWorkspaceId={options.activeWorkspaceId ? gatewayWorkspaceKey({ gateway_id: "gateway-test", workspace_id: options.activeWorkspaceId }) : null}
        searchOpen={options.searchOpen ?? false}
        searchQuery={options.searchQuery ?? ""}
        workspaceSwitching={false}
        startingWorkspaceIds={new Set()}
        removingWorkspaceIds={new Set()}
        onActivateWorkspace={options.onActivateWorkspace ?? (async () => undefined)}
        onSetWorkspaceParent={async () => undefined}
        onRefreshWorkspaceSessions={async () => undefined}
        onCreateSessionInFolder={async () => undefined}
        onSessionFolderDeleted={async () => undefined}
        catalogRefreshVersions={new Map()}
        onSelectSession={() => undefined}
        onCloseSearch={() => undefined}
        onStatusChange={() => undefined}
        onOpenDiagnostic={(title, detail) => diagnostics.push({ title, detail })}
        onOpenWorkspaceMenu={() => undefined}
        onOpenSessionMenu={() => undefined}
        activeJobIdsBySession={new Map()}
        unreadSessionKeys={new Set()}
        onRequestAddWorkspace={() => undefined}
        onOpenConnectionManager={() => undefined}
        onReconnectWorkspace={async () => undefined}
        onStartWorkspace={async () => undefined}
      />,
    );
    await new Promise<void>((resolve) => setTimeout(resolve, 20));
  });
  return { html: JSON.stringify(tree.toJSON()), tree, diagnostics, refresh: () => actions!.refresh() };
}

function viewDiagnostic(tree: ReactTestRenderer, index = 0): void {
  const buttons = tree.root.findAllByType("button").filter((button) => button.props.children === "查看日志");
  act(() => buttons[index].props.onClick());
}

/** 展开第一个工作区行，触发真实 loadBranch。 */
async function expandFirstWorkspace(tree: ReactTestRenderer): Promise<void> {
  const chevrons = tree.root.findAll(
    (instance) =>
      typeof instance.props.className === "string"
      && instance.props.className.includes("session-resource-expand-toggle"),
    { deep: true },
  );
  await act(async () => {
    chevrons[0]?.props.onClick();
    await new Promise<void>((resolve) => setTimeout(resolve, 20));
  });
}

describe("工作区导航树对已消失工作区的渲染", () => {
  test("导航索引残留但工作区已不在列表时明确标记不可用并禁用激活", async () => {
    const { html } = await renderExplorer({
      nodes: [navigationNode(GONE_WORKSPACE_ID, "已删除的工作区")],
      workspaces: [],
    });
    expect(html).toContain("已不可用");
    expect(html).toContain("已删除的工作区");
    // 激活按钮必须被禁用，避免点到已不存在的 workspace_id
    expect(html).toContain('"disabled":true');
  });

  test("工作区仍在列表且就绪时不显示不可用提示", async () => {
    const { html } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "正常的工作区")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
    });
    expect(html).not.toContain("已不可用");
    expect(html).toContain("正常的工作区");
  });
});

describe("工作区导航树的空态与加载态", () => {
  test("导航已加载但没有任何工作区时给出明确空态而不是空树", async () => {
    const { html } = await renderExplorer({
      nodes: [],
      workspaces: [],
    });
    expect(html).toContain("还没有工作区");
    expect(html).toContain("添加工作区");
    expect(html).not.toContain("正在加载工作区目录");
  });

  test("导航尚未加载时显示加载态而不是空态", async () => {
    const { html } = await renderExplorer({
      nodes: [],
      workspaces: [],
      navigationPending: true,
    });
    expect(html).toContain('"aria-busy":true');
    expect(html).not.toContain("正在加载工作区目录");
    expect(html).not.toContain("还没有工作区");
  });

  test("导航加载失败时显示错误卡而不是空态", async () => {
    const { html, tree, diagnostics } = await renderExplorer({
      nodes: [],
      workspaces: [],
      navigationFails: "HTTP 500 内部错误",
    });
    expect(html).toContain("无法加载工作区列表");
    expect(html).not.toContain("HTTP 500 内部错误");
    viewDiagnostic(tree);
    expect(diagnostics[0]).toEqual({ title: "无法加载工作区列表", detail: "请求失败 500 : HTTP 500 内部错误；请求=/api/gateway/workspace-navigation" });
    expect(html).not.toContain("还没有工作区");
  });

  test("刷新失败但保留了空的旧导航快照时只显示错误卡，不并列空态", async () => {
    const { html } = await renderExplorer({
      nodes: [],
      workspaces: [],
      navigationFails: "HTTP 500 内部错误",
    });
    expect(html).toContain("无法加载工作区列表");
    // 旧快照为空 + 刷新失败：错误优先，空态必须让位
    expect(html).not.toContain("还没有工作区");
  });
});

describe("目录搜索分支诊断渲染", () => {
  test("后端没有 HTTP 状态时不展示 null，仍保留原分支错误", async () => {
    const { tree } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      searchOpen: true, searchQuery: "needle", searchDiagnostics: true, searchHttpStatus: null,
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    const html = JSON.stringify(tree.toJSON());
    for (const detail of ["code: owner_unavailable", "error_owner: search_transport", "reason: route_removed",
      "request_id: catalog-diagnostic-request"]) expect(html).toContain(detail);
    expect(html).not.toContain("http_status:");
  });

  test("将错误字段和 notice 当纯文本显示，不把命令链接变成可执行链接", async () => {
    const { tree } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      searchOpen: true,
      searchQuery: "needle",
      searchDiagnostics: true,
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    const html = JSON.stringify(tree.toJSON());
    for (const detail of ["code: owner_unavailable", "error_owner: search_transport", "reason: route_removed",
      "http_status: 503", "retryable: true", "request_id: catalog-diagnostic-request",
      "warning: [执行](command:workbench.action.danger)"]) {
      expect(html).toContain(detail);
    }
    expect(tree.root.findAllByType("a").some(link => String(link.props.href ?? "").startsWith("command:"))).toBe(false);
  });

  test("真实 SSE 校验省略 diagnostics 时仍显示分支错误", async () => {
    const { tree } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      searchOpen: true,
      searchQuery: "needle",
      searchDiagnostics: true,
      searchNotices: false,
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    const html = JSON.stringify(tree.toJSON());
    expect(html).toContain("code: owner_unavailable");
    expect(html).toContain("request_id: catalog-diagnostic-request");
  });

  test("一个 available warning 分支展开后仍能遍历省略 diagnostics 的分支", async () => {
    const { tree } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1"), navigationNode(OTHER_WORKSPACE_ID, "gw_2")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready"), gatewayWorkspace(OTHER_WORKSPACE_ID, "ready")],
      searchOpen: true,
      searchQuery: "needle",
      searchDiagnostics: true,
      searchMixedDiagnostics: true,
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    const html = JSON.stringify(tree.toJSON());
    expect(html).toContain("warning: [执行](command:workbench.action.danger)");
    expect(html).toContain("state: complete");
  });
});

describe("会话目录分支的加载失败与空态", () => {
  test("分支加载失败时显示错误卡并保留技术详情", async () => {
    const { tree, diagnostics } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      branchError: "HTTP 500 内部错误",
    });
    await expandFirstWorkspace(tree);
    expect(JSON.stringify(tree.toJSON())).toContain("无法读取工作区目录");
    expect(JSON.stringify(tree.toJSON())).not.toContain("HTTP 500 内部错误");
    viewDiagnostic(tree);
    expect(diagnostics[0].detail).toBe("重新读取会话目录失败: root: 请求失败 500 : HTTP 500 内部错误；请求=/api/v1/session-catalog/children?minimum_catalog_revision=0&limit=100");
  });

  test("空分支不插入占位行", async () => {
    const { tree } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
    });
    await expandFirstWorkspace(tree);
    expect(tree.root.findByProps({ role: "group" }).findAllByType("li")).toHaveLength(0);
    expect(JSON.stringify(tree.toJSON())).not.toContain("无法读取工作区目录");
  });

  test("工作区离线且带连接错误、分支读取成功但为空时，不得把空态与错误卡并列", async () => {
    const { tree } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "offline", "SSH 连接已断开")],
    });
    await expandFirstWorkspace(tree);
    const html = JSON.stringify(tree.toJSON());
    // branch.error 为空，但工作区自身连接错误必须优先：否则用户无法区分「真没会话」和「读不到」。
    expect(html).toContain("无法读取工作区目录");
  });

  test("节点缺 node_id 时显示可见错误卡，而不是走 React key 警告或无限递归", async () => {
    const { tree, diagnostics } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      branchItems: [{ kind: "folder", name: "坏文件夹", has_children: true }],
    });
    await expandFirstWorkspace(tree);
    const html = JSON.stringify(tree.toJSON());
    expect(html).toContain("无法读取工作区目录");
    viewDiagnostic(tree);
    expect(diagnostics[0].detail).toContain("node_id 必须是非空字符串");
  });

  test("同级节点重复 node_id 时显示可见错误卡", async () => {
    const { tree, diagnostics } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      branchItems: [
        { node_id: "dup", kind: "session", name: "甲", session_id: "s-a", has_children: false },
        { node_id: "dup", kind: "session", name: "乙", session_id: "s-b", has_children: false },
      ],
    });
    await expandFirstWorkspace(tree);
    const html = JSON.stringify(tree.toJSON());
    expect(html).toContain("无法读取工作区目录");
    viewDiagnostic(tree);
    expect(diagnostics[0].detail).toContain("重复的 node_id: dup");
  });
});

describe("会话目录更新保持树行稳定", () => {
  function catalogNode(nodeId: string, kind: "folder" | "session", parent: string | null = null): SessionCatalogNode {
    return {
      node_id: nodeId, kind, name: nodeId, parent_node_id: parent,
      folder_id: kind === "folder" ? nodeId : null,
      session_id: kind === "session" ? nodeId : null,
      has_children: nodeId === "parent", node_revision: 1, state: "active",
    };
  }

  function catalogPage(parent: string | null, items: SessionCatalogNode[]): SessionCatalogPage {
    return { revision: "catalog-3", catalog_revision: 3, parent_node_id: parent,
      sync: { gateway_id: "gateway-test", workspace_id: WORKSPACE_ID, source: "authoritative", stale: false },
      items, cursor: null, total: items.length, consistency_warning: null };
  }

  async function mountCatalog(kind: "folder" | "session", otherChildren = false, sourceCursor: string | null = null) {
    const pages = new Map([
      [null, catalogPage(null, [catalogNode("parent", kind), catalogNode("stable", "session")])],
      ["parent", catalogPage("parent", [catalogNode("child", "session", "parent"),
        ...(otherChildren ? [catalogNode("other", "session", "parent")] : [])])],
    ]);
    if (sourceCursor) {
      pages.get("parent")!.cursor = sourceCursor;
      pages.get("parent")!.total = 20;
    }
    const { tree, refresh } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1")], workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      activeWorkspaceId: WORKSPACE_ID,
      branchResponse: (parent, request) => catalogReadResponse(request, { ...pages.get(parent) }),
      catalogItems: [...pages.values()].flatMap((page) => page.items),
    });
    await act(async () => {
      tree.root.findByProps({ "aria-label": `展开${kind === "session" ? "会话" : "文件夹"} parent` }).props.onClick();
      await new Promise<void>((resolve) => setTimeout(resolve, 20));
    });

    const base = createCatalogOutbox({ gatewayId: "gateway-test", workspaceId: WORKSPACE_ID, principal: "local" });
    const props = tree.root.findByType(SessionResourceExplorer).props as React.ComponentProps<typeof SessionResourceExplorer>;
    const updateOutbox = async (outbox: typeof base) => {
      await act(async () => {
        tree.update(<SessionResourceExplorer {...props}
          sessionCatalogOutboxSnapshot={new Map([[sessionCatalogOutboxScopeKey(WORKSPACE_KEY), {
            gatewayWorkspaceId: WORKSPACE_KEY, principal: "local", partitionKey: catalogOutboxPartitionKey(outbox.partition), outbox,
          }]])} />);
      });
    };
    return { tree, pages, base, updateOutbox, refresh };
  }

  test("会话图标显示展开状态，折叠与展开同步更新提示和子节点", async () => {
    const { tree } = await mountCatalog("session");
    const toggle = () => tree.root.findByProps({ className: "session-resource-expand-toggle", "aria-expanded": true, "aria-label": "折叠会话 parent" });
    expect(toggle().props.title).toBe("折叠会话 parent");
    expect(toggle().findByType("span").props.className).toBe("codicon codicon-chevron-down");
    expect(tree.root.findAllByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-child` })).toHaveLength(1);
    expect(tree.root.findByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-stable` }).findByProps({ className: "session-resource-expand-toggle" }).props['aria-expanded']).toBe(false);
    await act(async () => toggle().props.onClick());
    const collapsed = tree.root.findByProps({ "aria-label": "展开会话 parent" });
    expect(collapsed.props["aria-expanded"]).toBe(false);
    expect(collapsed.props.title).toBe("展开会话 parent");
    expect(collapsed.findByType("span").props.className).toBe("codicon codicon-chevron-right");
    expect(tree.root.findAllByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-child` })).toHaveLength(0);
    await act(async () => collapsed.props.onClick());
    expect(toggle().findByType("span").props.className).toBe("codicon codicon-chevron-down");
    expect(tree.root.findAllByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-child` })).toHaveLength(1);
    act(() => tree.unmount());
  });

  for (const kind of ["folder", "session"] as const) {
    for (const otherChildren of [false, true]) {
      test(`${kind} 移出${otherChildren ? "其中一个" : "最后一个"}子项不插入提示行，回退保留原展开状态`, async () => {
        const { tree, base, updateOutbox } = await mountCatalog(kind, otherChildren);
        const stable = tree.root.findByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-stable` });
        const moving = addCatalogOutboxIntent(base, "op_00000000000000000000000000000001",
          { kind: "move_node", targetNodeId: "child", parentNodeId: null },
          { baseCatalogRevision: 3, expectedRevision: 1 });
        await updateOutbox(moving);
        expect(tree.root.findAllByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-child` })).toHaveLength(1);
        expect(tree.root.findByProps({ "data-pending-state": "pending_local" })).toBeDefined();
        expect(tree.root.findByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-stable` })).toBe(stable);
        const parent = tree.root.findByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-parent` }).parent!;
        expect(parent.props["aria-expanded"]).toBe(true);
        expect(parent.findAllByProps({ role: "treeitem" }).filter((item) => item !== parent)).toHaveLength(otherChildren ? 1 : 0);
        expect(JSON.stringify(tree.toJSON())).not.toContain("正在加载…");
        await updateOutbox(base);
        expect(tree.root.findByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-parent` }).parent!.props["aria-expanded"]).toBe(true);
        expect(parent.findAllByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-child` })).toHaveLength(1);
        expect(tree.root.findByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-stable` })).toBe(stable);
        act(() => tree.unmount());
      });
    }
  }

  test("已加载子项全部移出但仍有续页时保留分页入口，不误报空目录", async () => {
    const { tree, base, updateOutbox } = await mountCatalog("folder", false, "next-page");
    await updateOutbox(addCatalogOutboxIntent(base, "op_00000000000000000000000000000001",
      { kind: "move_node", targetNodeId: "child", parentNodeId: null },
      { baseCatalogRevision: 3, expectedRevision: 1 }));
    expect(tree.root.findByProps({ className: "session-resource-load-more" }).children.join("")).toBe("加载更多（0/20）");
    act(() => tree.unmount());
  });

  test("分页总数未知时保留空总数，不显示伪造的零", async () => {
    const { tree } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      branchResponse: (parent, request) => catalogReadResponse(request, { ...catalogPage(parent, []), cursor: "next-page", total: null }),
    });
    try {
      await expandFirstWorkspace(tree);
      expect(tree.root.findByProps({ className: "session-resource-load-more" }).children.join("")).toBe("加载更多（0/）");
    } finally {
      act(() => tree.unmount());
    }
  });

  test("首次展开保留加载提示，空结果返回后移除提示行", async () => {
    const deferred = createDeferredResult<Response>();
    const { tree } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1")], workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      branchResponse: () => deferred.promise,
    });
    await expandFirstWorkspace(tree);
    expect(JSON.stringify(tree.toJSON())).toContain("正在加载…");
    await act(async () => deferred.release(apiSuccessResponse(catalogPage(null, []))));
    expect(tree.root.findByProps({ role: "group" }).findAllByType("li")).toHaveLength(0);
    act(() => tree.unmount());
  });

  test("后台刷新保留行身份与展开状态，不追加加载行", async () => {
    const { tree, pages, refresh } = await mountCatalog("folder");
    const child = tree.root.findByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-child` });
    const deferred = createDeferredResult<Response>();
    installGatewayFetch((request) => {
      const { path } = request;
      if (path === "/api/gateway/workspace-navigation") {
        return apiSuccessResponse({ revision: "navigation", nodes: [navigationNode(WORKSPACE_ID, "gw_1")] });
      }
      if (path === "/api/v1/session-catalog/export") {
        return catalogReadResponse(request, { revision: "catalog-3", catalog_revision: 3,
          items: [catalogNode("parent", "folder"), catalogNode("stable", "session"), catalogNode("child", "session", "parent")] });
      }
      if (path === "/api/v1/session-catalog/refresh") return apiSuccessResponse(pages.get(null));
      if (path === "/api/v1/session-catalog/children") return deferred.promise;
      return undefined;
    });
    await act(async () => {
      refresh();
      await new Promise<void>((resolve) => setTimeout(resolve, 0));
    });
    // 根分支和展开分支一起刷新，结果在同一批次发布前均保持 busy。
    expect(tree.root.findAllByProps({ role: "group", "aria-busy": true })).toHaveLength(2);
    expect(JSON.stringify(tree.toJSON())).not.toContain("正在加载…");
    expect(tree.root.findByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-child` })).toBe(child);
    await act(async () => {
      deferred.release(apiSuccessResponse(catalogPage("parent", [catalogNode("child", "session", "parent")])));
    });
    expect(tree.root.findAllByProps({ role: "group", "aria-busy": true })).toHaveLength(0);
    expect(tree.root.findByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-child` })).toBe(child);
    act(() => tree.unmount());
  });

  test("后端确认会话无子项后保留统一展开图标，不插入空态行", async () => {
    const { tree } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "gw_1")], workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      branchResponse: (parent, request) => catalogReadResponse(request, { ...catalogPage(parent, parent === null ? [catalogNode("parent", "session")] : []) }),
      catalogItems: [catalogNode("parent", "session")],
    });
    await expandFirstWorkspace(tree);
    await act(async () => {
      tree.root.findByProps({ "aria-label": "展开会话 parent" }).props.onClick();
      await new Promise<void>((resolve) => setTimeout(resolve, 20));
    });
    const toggle = tree.root.findByProps({ "aria-label": "折叠会话 parent" });
    expect(toggle.props['aria-expanded']).toBe(true);
    expect(toggle.parent!.parent!.findByProps({ role: "group" }).findAllByType("li")).toHaveLength(0);
    act(() => tree.unmount());
  });
});

describe("定位当前会话失败的错误归属", () => {
  test("导航加载成功但定位当前会话失败时，不得误报工作区列表不可用", async () => {
    const { html, tree, diagnostics } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "正常的工作区")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      activeWorkspaceId: WORKSPACE_ID,
      currentSessionId: "s-hidden",
      breadcrumbFails: "面包屑读取炸了",
    });
    // 工作区导航本身读到了，仍应正常渲染工作区行。
    expect(html).toContain("正常的工作区");
    // 定位失败必须走独立错误卡，而不是宣称整个工作区列表不可用。
    expect(html).not.toContain("无法加载工作区列表");
    expect(html).toContain("无法定位当前会话");
    expect(html).not.toContain("面包屑读取炸了");
    viewDiagnostic(tree);
    expect(diagnostics[0].detail).toContain("面包屑读取炸了");
  });
});

describe("工作区错误提示合并与恢复", () => {
  test("切换与目录失败在同一工作区合并，日志保留两个请求的详情", async () => {
    const { tree, diagnostics } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "目标工作区")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      branchError: "请求超时: /api/v1/session-catalog/children",
      onActivateWorkspace: async () => { throw new Error(`请求超时: /api/gateway/workspaces/${WORKSPACE_ID}/activate`); },
    });
    await expandFirstWorkspace(tree);
    await act(async () => {
      tree.root.findByProps({ className: "session-resource-label workspace-label" }).props.onClick();
    });
    expect(tree.root.findAllByProps({ role: "alert" })).toHaveLength(1);
    expect(JSON.stringify(tree.toJSON())).toContain("切换工作区失败");
    expect(JSON.stringify(tree.toJSON())).not.toContain("请求超时:");
    viewDiagnostic(tree);
    expect(diagnostics[0].title).toContain(WORKSPACE_ID);
    expect(diagnostics[0].detail).toContain(`/api/gateway/workspaces/${WORKSPACE_ID}/activate`);
    expect(diagnostics[0].detail).toContain("/api/v1/session-catalog/children");
    act(() => tree.unmount());
  });

  test("目标未展开时切换失败仍可见，重试成功后清除提示", async () => {
    let attempts = 0;
    const { tree } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "目标工作区")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      onActivateWorkspace: async () => {
        if (++attempts === 1) throw new Error("网络已断开");
      },
    });
    await act(async () => {
      tree.root.findByProps({ className: "session-resource-label workspace-label" }).props.onClick();
    });
    expect(tree.root.findAllByProps({ role: "alert" })).toHaveLength(1);
    await act(async () => {
      tree.root.findAllByType("button").find((button) => button.props.children === "重试切换")!.props.onClick();
      await new Promise<void>((resolve) => setTimeout(resolve, 20));
    });
    expect(attempts).toBe(2);
    expect(tree.root.findAllByProps({ role: "alert" })).toHaveLength(0);
    act(() => tree.unmount());
  });

  test("英文下重试切换失败保留激活入口，激活成功后目录失败改为只重读目录", async () => {
    setLocaleResources("en", english);
    const activations: string[] = [];
    let catalogReads = 0;
    const { tree } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "用户保留的工作区名")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready")],
      onActivateWorkspace: async (workspaceId) => {
        activations.push(workspaceId);
        if (activations.length < 3) throw new Error("原始激活诊断");
      },
      branchResponse: (parent, request) => ++catalogReads === 1
        ? apiErrorResponse("原始目录诊断")
        : catalogReadResponse(request, { revision: "catalog-3", catalog_revision: 3, parent_node_id: parent,
          items: [], cursor: null, total: 0, consistency_warning: null }),
    });
    const press = async (label: string) => {
      await act(async () => {
        tree.root.findAllByType("button").find((button) => button.props.children === label)!.props.onClick();
        await new Promise<void>((resolve) => setTimeout(resolve, 20));
      });
    };
    try {
      await act(async () => {
        tree.root.findByProps({ className: "session-resource-label workspace-label" }).props.onClick();
      });
      expect(JSON.stringify(tree.toJSON())).toContain("Switching workspace failed");
      expect(JSON.stringify(tree.toJSON())).toContain("用户保留的工作区名");
      await press("Retry switch");
      expect(activations).toEqual([WORKSPACE_KEY, WORKSPACE_KEY]);
      expect(catalogReads).toBe(0);
      expect(JSON.stringify(tree.toJSON())).toContain("Retry switch");

      await press("Retry switch");
      expect(activations).toEqual([WORKSPACE_KEY, WORKSPACE_KEY, WORKSPACE_KEY]);
      expect(catalogReads).toBe(1);
      expect(JSON.stringify(tree.toJSON())).toContain("Refreshing the session catalog failed");
      expect(JSON.stringify(tree.toJSON())).not.toContain("Retry switch");

      await press("Reload catalog");
      expect(activations).toHaveLength(3);
      expect(catalogReads).toBe(2);
      expect(tree.root.findAllByProps({ role: "alert" })).toHaveLength(0);
    } finally {
      act(() => tree.unmount());
    }
  });

  test("另一工作区的目录错误仍独立显示，刷新不叠加全局错误横幅", async () => {
    const { tree, refresh } = await renderExplorer({
      nodes: [navigationNode(WORKSPACE_ID, "甲"), navigationNode(OTHER_WORKSPACE_ID, "乙")],
      workspaces: [gatewayWorkspace(WORKSPACE_ID, "ready"), gatewayWorkspace(OTHER_WORKSPACE_ID, "ready")],
      branchError: "目录请求失败",
      activeWorkspaceId: OTHER_WORKSPACE_ID,
      onActivateWorkspace: async () => { throw new Error("切换请求失败"); },
    });
    await act(async () => {
      const collapsedWorkspaces = tree.root.findAllByType("button").filter((button) =>
        String(button.props["aria-label"]).startsWith("展开工作区 "));
      for (const button of collapsedWorkspaces) button.props.onClick();
      await new Promise<void>((resolve) => setTimeout(resolve, 20));
    });
    await act(async () => {
      tree.root.findAllByProps({ className: "session-resource-label workspace-label" })[0].props.onClick();
      refresh();
      await new Promise<void>((resolve) => setTimeout(resolve, 20));
    });
    expect(tree.root.findAllByProps({ role: "alert" })).toHaveLength(2);
    const text = JSON.stringify(tree.toJSON());
    expect(text).toContain("切换工作区失败");
    expect(text).toContain("无法读取工作区目录");
    expect(text).not.toContain("session-resource-error-card global session-resource-error");
    act(() => tree.unmount());
  });
});

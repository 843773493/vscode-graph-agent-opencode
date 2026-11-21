import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { GatewayWorkspace } from "../../types/backend";
import {
  apiSuccessResponse,
  catalogReadResponse,
  installGatewayFetch,
  restoreGatewayTestEnvironment,
  type GatewayFetchHandler,
} from "../../tests/fixtures/session/gatewayEnvironment";
import { installGatewayTestWindow } from "../../tests/fixtures/session/gatewayBrowserGlobals";
import { createDeferredResult, flushReactEffects } from "../../tests/fixtures/session/asyncControl";
import SessionResourceExplorer from "./SessionResourceExplorer";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { FederatedSearchEventDTO } from "../../types/protocol_generated/boxteam/search/v1/public";

const WORKSPACE_ID = "00000000-0000-0000-0000-000000000001";
const WORKSPACE_KEY = gatewayWorkspaceKey({ gateway_id: "gateway-test", workspace_id: WORKSPACE_ID });

afterEach(restoreGatewayTestEnvironment);


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

function gatewayWorkspace(workspaceId: string): GatewayWorkspace {
  return { gateway_id: "gateway-test", workspace_id: workspaceId, parent_workspace: null, name: workspaceId, backend_url: "http://127.0.0.1:9000", connection_kind: "local", status: "ready", active: false, managed: true, affected_workspace_ids: [], removable: true, system_default: false, runtime_action: "safe_restart_managed_backend", remote_gateways: [], services: {}, connection_error: null, checked_at: "2026-07-31T00:00:00Z" };
}

/** 渲染导航树 + 一个契约被破坏的会话目录节点，展开工作区后返回渲染器。 */
async function renderWithBranchItem(
  branchItems: unknown[],
  onOpenDiagnostic: (title: string, detail: string) => void,
  options: { props?: Partial<React.ComponentProps<typeof SessionResourceExplorer>>; respond?: GatewayFetchHandler } = {},
): Promise<ReactTestRenderer> {
  installGatewayTestWindow(80_141);
  installGatewayFetch((request) => {
    const claimed = options.respond?.(request);
    if (claimed !== undefined) return claimed;
    if (request.path.includes("/api/gateway/workspace-navigation")) {
      return apiSuccessResponse({ revision: "navigation", nodes: [navigationNode(WORKSPACE_ID, "工作区")] });
    }
    if (request.path.includes("/api/v1/session-catalog/children")) {
      return catalogReadResponse(request, {
        revision: "catalog",
        catalog_revision: 3,
        parent_node_id: null,
        items: branchItems,
        cursor: null,
        total: branchItems.length,
      });
    }
    return undefined;
  });

  let tree!: ReactTestRenderer;
  await act(async () => {
    tree = create(
      <SessionResourceExplorer
        apiPort={80_141}
        workspaces={[gatewayWorkspace(WORKSPACE_ID)]}
        activeWorkspaceId={null}
        currentSessionId=""
        currentSessionWorkspaceId={null}
        selectedSessionKey={null}
        searchOpen={false}
        searchQuery=""
        workspaceSwitching={false}
        startingWorkspaceIds={new Set()}
        removingWorkspaceIds={new Set()}
        onActivateWorkspace={async () => undefined}
        onSetWorkspaceParent={async () => undefined}
        onRefreshWorkspaceSessions={async () => undefined}
        onCreateSessionInFolder={async () => undefined}
        onSessionFolderDeleted={async () => undefined}
        catalogRefreshVersions={new Map()}
        onSelectSession={() => undefined}
        onCloseSearch={() => undefined}
        onStatusChange={() => undefined}
        onOpenDiagnostic={onOpenDiagnostic}
        onOpenWorkspaceMenu={() => undefined}
        onOpenSessionMenu={() => undefined}
        activeJobIdsBySession={new Map()}
        unreadSessionKeys={new Set()}
        onRequestAddWorkspace={() => undefined}
        onOpenConnectionManager={() => undefined}
        onReconnectWorkspace={async () => undefined}
        onStartWorkspace={async () => undefined}
        {...options.props}
      />,
    );
    await new Promise<void>((resolve) => setTimeout(resolve, 20));
  });

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
  return tree;
}

function catalogNodeRow(tree: ReactTestRenderer, nodeId: string) {
  return tree.root.findByProps({ "data-testid": `catalog-node-${WORKSPACE_KEY}-${nodeId}` });
}

test("树选择与已读取会话独立，后端迟到更新不抢回高亮", async () => {
  const nodes = ["ses_a", "ses_b"].map(sessionId => ({ node_id: sessionId, session_id: sessionId,
    kind: "session", name: sessionId, has_children: false, node_revision: 1 }));
  const tree = await renderWithBranchItem(nodes, () => undefined, {
    props: { selectedSessionKey: sessionScopeKey(WORKSPACE_KEY, "ses_b") },
  });
  try {
    const selected = (id: string) => catalogNodeRow(tree, id).parent?.props["aria-selected"];
    expect(selected("ses_a")).toBe(false);
    expect(selected("ses_b")).toBe(true);
    expect(catalogNodeRow(tree, "ses_b").props.className).toContain("current-session");
    const props = tree.root.findByType(SessionResourceExplorer).props;
    act(() => tree.update(<SessionResourceExplorer {...props} currentSessionWorkspaceId={WORKSPACE_KEY} currentSessionId="ses_b" />));
    act(() => tree.update(<SessionResourceExplorer {...props} currentSessionWorkspaceId={WORKSPACE_KEY} currentSessionId="ses_a" />));
    expect(catalogNodeRow(tree, "ses_a").props["aria-current"]).toBe("true");
    expect(selected("ses_b")).toBe(true);
    // 同名 Session 在其它工作区不能获得当前树的选择。
    act(() => tree.update(<SessionResourceExplorer {...props} selectedSessionKey={sessionScopeKey("other-workspace", "ses_b")} />));
    expect(selected("ses_b")).toBe(false);
    // 切换文件编辑器清除选择，无需等待 Session 补读。
    act(() => tree.update(<SessionResourceExplorer {...props} selectedSessionKey={null} />));
    expect(selected("ses_a")).toBe(false);
    expect(selected("ses_b")).toBe(false);
  } finally { act(() => tree.unmount()); }
});

test("无子节点会话使用相同展开入口，空目录不混入 Thread，名称独立打开会话", async () => {
  const session = { session_id: "ses_threads", workspace_id: WORKSPACE_ID, thread_id: "thr_main", title: "独立会话",
    resource_identity: "session:ses_threads", uri: "boxteam://gateway-test/workspace/test/resources/session/ses_threads",
    scope: "workspace", current_agent_id: "default", parent_session_id: null,
    created_at: "2026-11-17T00:00:00Z", updated_at: "2026-11-17T00:00:00Z" };
  let catalogReads = 0;
  const openedSessions: string[] = [];
  const node = { node_id: session.session_id, session_id: session.session_id,
    kind: "session", name: session.title, has_children: false, node_revision: 1, session };
  const tree = await renderWithBranchItem([node], () => undefined, {
    respond: request => {
      if (request.path.endsWith('/session-catalog/export')) {
        return catalogReadResponse(request, { revision: "catalog", catalog_revision: 3, items: [node] });
      }
      if (!request.path.includes('/api/v1/session-catalog/children')) return undefined;
      catalogReads += 1;
      if (new URL(request.url, "http://localhost").searchParams.get("parent_node_id") !== session.session_id) return undefined;
      return catalogReadResponse(request, { revision: "catalog", catalog_revision: 3, parent_node_id: session.session_id, items: [], cursor: null, total: 0 });
    },
    props: { onSelectSession: (_workspaceId, sessionId) => { openedSessions.push(sessionId); } },
  });
  try {
    const row = catalogNodeRow(tree, session.session_id);
    const initialReads = catalogReads;
    const toggle = row.findByProps({ className: "session-resource-expand-toggle" });
    expect(toggle.props['aria-expanded']).toBe(false);
    expect(row.parent?.props.role).toBe("treeitem");
    expect(row.parent?.props['aria-expanded']).toBe(false);
    const mainAction = row.findByProps({ className: "session-resource-main-action" });
    expect(toggle.parent).toBe(row);
    expect(row.props.onClick).toBeUndefined();
    expect(mainAction.findAllByProps({ "aria-hidden": true })).toHaveLength(0);
    expect(catalogReads).toBe(initialReads);
    await act(async () => { toggle.props.onClick(); await flushReactEffects(); });
    expect(toggle.props['aria-expanded']).toBe(true);
    expect(toggle.props.title).toBe(`折叠会话 ${session.title}`);
    expect(row.parent?.findByProps({ role: "group" }).findAllByType("li")).toHaveLength(0);
    expect(catalogReads).toBe(initialReads + 1);
    await act(async () => { toggle.props.onClick(); await flushReactEffects(); });
    expect(toggle.props['aria-expanded']).toBe(false);
    expect(toggle.props.title).toBe(`展开会话 ${session.title}`);
    expect(openedSessions).toEqual([]);
    await act(async () => { mainAction.props.onClick(); await flushReactEffects(); });
    expect(openedSessions).toEqual([session.session_id]);
    await act(async () => { await flushReactEffects(); });
    expect(catalogReads).toBe(initialReads + 1);
  } finally { act(() => tree.unmount()); }
});

/** 右键菜单的可见失败出口：会话资源区的 actionError 横幅。 */
function actionErrorText(tree: ReactTestRenderer): string {
  const banners = tree.root.findAll(
    (instance) =>
      typeof instance.props.className === "string"
      && instance.props.className.split(" ").includes("session-resource-error"),
  );
  // 操作失败保留简短标题，技术诊断通过输出入口查看。
  return banners
    .flatMap((banner) => banner.findAllByType("strong"))
    .map((span) => span.props.children)
    .filter((child): child is string => typeof child === "string")
    .join(" ");
}

test("未确认目录镜像保留节点并展示确认时间、原错误及重新读取入口", async () => {
  const confirmedAt = "2026-11-13T10:00:00Z";
  const node = { node_id: "folder_cached", folder_id: "folder_cached", kind: "folder", name: "离线目录", has_children: false, node_revision: 1 };
  let reads = 0;
  const tree = await renderWithBranchItem([], () => undefined, {
    respond: (request) => {
      if (request.path !== "/api/v1/session-catalog/children") return undefined;
      reads += 1;
      return catalogReadResponse(request, {
        revision: "catalog-cached", catalog_revision: 3, parent_node_id: null,
        items: [node], total: 1, cursor: null,
        sync: { gateway_id: "gateway-test", workspace_id: WORKSPACE_ID, source: "gateway_mirror", stale: true,
          authoritative_confirmed_at: confirmedAt, error: "目录确认连接已断开" },
      });
    },
  });
  const notice = tree.root.findAll((instance) => instance.type === "li" && instance.props.role === "status")
    .find((instance) => instance.findAllByType("strong").some((title) => title.props.children === "目录镜像尚未确认"));
  expect(notice).toBeDefined();
  expect(notice!.findAllByType("span").map((span) => span.props.children).join(" ")).toContain(confirmedAt);
  expect(notice!.findAllByType("span").map((span) => span.props.children).join(" ")).toContain("目录确认连接已断开");
  expect(catalogNodeRow(tree, node.node_id)).toBeDefined();
  const beforeReload = reads;
  await act(async () => {
    notice!.findByType("button").props.onClick();
    await flushReactEffects();
  });
  expect(reads).toBeGreaterThan(beforeReload);
  act(() => tree.unmount());
});

describe("会话目录节点右键菜单的契约破坏", () => {
  test("文件夹节点缺 folder_id 时响亮报错，而不是右键静默无反应", async () => {
    const diagnostics: string[] = [];
    const tree = await renderWithBranchItem([
      { node_id: "ses_broken_folder", kind: "folder", name: "坏文件夹", has_children: true },
    ], (_title, detail) => diagnostics.push(detail));
    expect(actionErrorText(tree)).toBe("");

    act(() => {
      catalogNodeRow(tree, "ses_broken_folder").props.onContextMenu({
        preventDefault() {},
        stopPropagation() {},
        clientX: 10,
        clientY: 10,
      });
    });

    expect(actionErrorText(tree)).toContain("失败");
    act(() => tree.root.findAllByType("button").find((button) => button.props.children === "查看日志")!.props.onClick());
    expect(diagnostics[0]).toContain("folder_id");
    act(() => tree.unmount());
  });

  test("会话节点缺 session_id 时响亮报错，而不是右键静默无反应", async () => {
    const diagnostics: string[] = [];
    const tree = await renderWithBranchItem([
      { node_id: "ses_broken", kind: "session", name: "坏会话", has_children: false },
    ], (_title, detail) => diagnostics.push(detail));

    act(() => {
      catalogNodeRow(tree, "ses_broken").props.onContextMenu({
        preventDefault() {},
        stopPropagation() {},
        clientX: 10,
        clientY: 10,
      });
    });

    expect(actionErrorText(tree)).toContain("失败");
    act(() => tree.root.findAllByType("button").find((button) => button.props.children === "查看日志")!.props.onClick());
    expect(diagnostics[0]).toContain("会话 ID");
    act(() => tree.unmount());
  });
});

test("真实搜索流按完整 owner 隔离同 UUID 会话的运行、未读与离线摘要", async () => {
  const first = gatewayWorkspace(WORKSPACE_ID);
  const second = { ...first, gateway_id: "gateway-other" };
  const owners = [first, second].map(({ gateway_id, workspace_id }) => ({ gateway_id, workspace_id }));
  const sessionId = "ses_shared_search";
  const started = createDeferredResult<ReadableStreamDefaultController<Uint8Array>>();
  const requestId = "catalog-search-components";
  let searchCalls = 0;
  const tree = await renderWithBranchItem([], () => undefined, {
    props: { workspaces: [first, second], searchOpen: true, searchQuery: "同名会话",
      activeJobIdsBySession: new Map([[sessionScopeKey(gatewayWorkspaceKey(first), sessionId), "job-first"]]),
      unreadSessionKeys: new Set([sessionScopeKey(gatewayWorkspaceKey(second), sessionId)]) },
    respond: ({ path, init }) => {
      if (path === "/api/gateway/search/workspaces") return apiSuccessResponse({ items: [], folders: [], complete: true });
      if (path !== "/api/gateway/search") return undefined;
      searchCalls += 1;
      expect(JSON.parse(String(init?.body))).toMatchObject({ scope: { mode: "all", owners: [] }, queries: [{ category: "catalog" }] });
      return new Response(new ReadableStream<Uint8Array>({ start: started.release }),
        { headers: { "Content-Type": "text/event-stream", "X-Request-ID": requestId } });
    },
  });
  try {
    await act(async () => {
      const output = await started.promise;
      let sequence = 0;
      const send = (type: string, payload: Record<string, unknown>) => {
        const event = FederatedSearchEventDTO.create({ type, sequence: ++sequence, request_id: requestId, ...payload });
        output.enqueue(new TextEncoder().encode(`id: ${sequence}\nevent: search\ndata: ${JSON.stringify(event)}\n\n`));
      };
      const branches = owners.map(owner => ({ branch: { owner, category: "catalog", operation: "name" },
        state: "complete", retryable: false, allocated_results: 50, returned_results: 1 }));
      const resolvedAt = "2026-11-05T00:00:00Z";
      send("scope", { scope: { scope: { version: 1, gateway_id: first.gateway_id, resolved_at: resolvedAt, mode: "all", owners },
        branches: owners.map(owner => ({ owner, state: "registered" })) } });
      for (const [index, owner] of owners.entries()) {
        const node = { node_id: sessionId, kind: "session", session_id: sessionId, name: `结果 ${index}`, node_revision: 1, state: "active" };
        send("page", { branch: branches[index], page: { provider_id: "session-catalog", workspace_id: owner.workspace_id,
          category: "catalog", operation: "name", contract_version: 1, state: "complete",
          catalog: { revision: "catalog-1", catalog_revision: 1, total: 1,
            items: [{ node, breadcrumb: [node], relative_path: node.name }] } },
          source: { kind: "gateway_catalog_mirror", stale: true, updated_at: resolvedAt } });
        send("branch", { branch: branches[index] });
      }
      send("end", { summary: { state: "complete", order: "category-scope-owner-v1", returned_results: 2, branches } });
      output.close();
      await flushReactEffects();
    });
    const rows = tree.root.findAllByProps({ className: "session-resource-search-result" });
    expect(searchCalls).toBe(1);
    expect(rows.map(row => row.findByType("strong").props.children)).toEqual(["结果 0", "结果 1"]);
    expect(rows[0].findAllByProps({ "aria-label": "会话正在运行" })).toHaveLength(1);
    expect(rows[0].findAllByProps({ "aria-label": "会话有未读结果" })).toHaveLength(0);
    expect(rows[1].findAllByProps({ "aria-label": "会话正在运行" })).toHaveLength(0);
    expect(rows[1].findAllByProps({ "aria-label": "会话有未读结果" })).toHaveLength(1);
    const summary = tree.root.findByProps({ className: "session-resource-offline-summary" });
    expect(summary.props.children[1].map((row: React.ReactElement) => row.key)).toEqual(owners.map(gatewayWorkspaceKey));
  } finally {
    act(() => tree.unmount());
  }
});

import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { GatewayWorkspace } from "../../types/backend";
import type { SessionGeneratorResourcesController } from "../../hooks/sessionResourceExplorer/useSessionGeneratorResources";
import {
  apiResponse,
  installGatewayFetch,
  installTestWindow,
  restoreSessionHookGlobals,
} from "../../hooks/session/sessionHookTestFixtures";
import SessionResourceExplorer from "./SessionResourceExplorer";

// 用真实 useSessionResourceExplorer + fetch 桩渲染导航树。不再替换 hook 与浮层模块：
// bun 的 mock.module 是进程级且不可撤销的，会污染同一进程后续所有测试文件看到的
// 这两个模块。

afterEach(restoreSessionHookGlobals);

const generatorResources = {
  generators: null,
  generationRuns: new Map(),
  generatorError: null,
} as unknown as SessionGeneratorResourcesController;

/** 带可读 detail 的错误信封：HttpRequestError 从顶层 detail/message 取诊断文本。 */
function errorResponse(detail: string): Response {
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
    workspace_id: workspaceId,
    position: 0,
    parent_path: null,
  };
}

function gatewayWorkspace(
  workspaceId: string,
  status: "ready" | "offline",
  connectionError: string | null = null,
): GatewayWorkspace {
  return {
    workspace_id: workspaceId,
    parent_workspace_id: null,
    name: workspaceId,
    root_path: "/home/test",
    backend_url: "http://127.0.0.1:9000",
    connection_kind: "local",
    status,
    active: false,
    managed: true,
    removable: true,
    system_default: false,
    runtime_action: "safe_restart_managed_backend",
    remote: null,
    services: {},
    connection_error: connectionError,
    checked_at: "2026-07-31T00:00:00Z",
  };
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
  /** 激活工作区与当前会话：用于触发「定位当前会话」链路。 */
  activeWorkspaceId?: string | null;
  currentSessionId?: string;
  breadcrumbFails?: string | null;
}): Promise<{ html: string; tree: ReactTestRenderer }> {
  installTestWindow(8014);
  installGatewayFetch((request) => {
    if (request.path.includes("/api/gateway/workspace-navigation")) {
      if (options.navigationFails) {
        return errorResponse(options.navigationFails);
      }
      if (options.navigationPending) {
        return new Promise<Response>(() => {});
      }
      if (options.navigationMissing) {
        return undefined;
      }
      return apiResponse({ revision: "navigation", nodes: options.nodes });
    }
    if (request.path.includes("/api/v1/session-catalog/breadcrumb/")) {
      if (options.breadcrumbFails) {
        return errorResponse(options.breadcrumbFails);
      }
      return apiResponse({ items: [] });
    }
    if (request.path.includes("/api/v1/session-catalog/children")) {
      if (options.branchError) {
        return errorResponse(options.branchError);
      }
      return apiResponse({
        revision: "catalog",
        parent_node_id: null,
        items: options.branchItems ?? [],
        cursor: null,
        total: 0,
      });
    }
    if (request.path.includes("/api/gateway/session-generators")) {
      return apiResponse({ revision: "generators", items: [] });
    }
    return undefined;
  });

  let tree!: ReactTestRenderer;
  await act(async () => {
    tree = create(
      <SessionResourceExplorer
        apiPort={8014}
        workspaces={options.workspaces}
        activeWorkspaceId={options.activeWorkspaceId ?? null}
        currentSessionId={options.currentSessionId ?? ""}
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
        catalogSyncKeys={new Map()}
        catalogRefreshVersions={new Map()}
        onSelectSession={() => undefined}
        onStatusChange={() => undefined}
        onOpenWorkspaceMenu={() => undefined}
        onOpenSessionMenu={() => undefined}
        activeJobIdsBySession={new Map()}
        unreadSessionKeys={new Set()}
        onRequestAddWorkspace={() => undefined}
        onOpenConnectionManager={() => undefined}
        onReconnectWorkspace={async () => undefined}
        onStartWorkspace={async () => undefined}
        generatorResources={generatorResources}
      />,
    );
    await new Promise<void>((resolve) => setTimeout(resolve, 20));
  });
  return { html: JSON.stringify(tree.toJSON()), tree };
}

/** 展开第一个工作区行，触发真实 loadBranch。 */
async function expandFirstWorkspace(tree: ReactTestRenderer): Promise<void> {
  const chevrons = tree.root.findAll(
    (instance) =>
      typeof instance.props.className === "string"
      && instance.props.className.includes("session-resource-chevron"),
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
      nodes: [navigationNode("gw_gone", "已删除的工作区")],
      workspaces: [],
    });
    expect(html).toContain("已不可用");
    expect(html).toContain("已删除的工作区");
    // 激活按钮必须被禁用，避免点到已不存在的 workspace_id
    expect(html).toContain('"disabled":true');
  });

  test("工作区仍在列表且就绪时不显示不可用提示", async () => {
    const { html } = await renderExplorer({
      nodes: [navigationNode("gw_ok", "正常的工作区")],
      workspaces: [gatewayWorkspace("gw_ok", "ready")],
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
    expect(html).toContain("正在加载工作区目录");
    expect(html).not.toContain("还没有工作区");
  });

  test("导航加载失败时显示错误卡而不是空态", async () => {
    const { html } = await renderExplorer({
      nodes: [],
      workspaces: [],
      navigationFails: "HTTP 500 内部错误",
    });
    expect(html).toContain("无法加载工作区列表");
    expect(html).toContain("HTTP 500 内部错误");
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

describe("会话目录分支的加载失败与空态", () => {
  test("分支加载失败时显示错误卡并保留技术详情", async () => {
    const { html, tree } = await renderExplorer({
      nodes: [navigationNode("gw_1", "gw_1")],
      workspaces: [gatewayWorkspace("gw_1", "ready")],
      branchError: "HTTP 500 内部错误",
    });
    await expandFirstWorkspace(tree);
    expect(JSON.stringify(tree.toJSON())).toContain("无法读取工作区目录");
    expect(JSON.stringify(tree.toJSON())).toContain("HTTP 500 内部错误");
    // 失败时不得同时宣称「暂无会话」，否则用户无法区分空目录和加载失败
    expect(JSON.stringify(tree.toJSON())).not.toContain("暂无会话或会话文件夹");
  });

  test("分支为空且无错时才显示暂无会话", async () => {
    const { tree } = await renderExplorer({
      nodes: [navigationNode("gw_1", "gw_1")],
      workspaces: [gatewayWorkspace("gw_1", "ready")],
    });
    await expandFirstWorkspace(tree);
    expect(JSON.stringify(tree.toJSON())).toContain("暂无会话或会话文件夹");
    expect(JSON.stringify(tree.toJSON())).not.toContain("无法读取工作区目录");
  });

  test("工作区离线且带连接错误、分支读取成功但为空时，不得把空态与错误卡并列", async () => {
    const { tree } = await renderExplorer({
      nodes: [navigationNode("gw_1", "gw_1")],
      workspaces: [gatewayWorkspace("gw_1", "offline", "SSH 连接已断开")],
    });
    await expandFirstWorkspace(tree);
    const html = JSON.stringify(tree.toJSON());
    // branch.error 为空，但工作区自身连接错误必须优先：否则用户无法区分「真没会话」和「读不到」。
    expect(html).toContain("无法读取工作区目录");
    expect(html).not.toContain("暂无会话或会话文件夹");
  });

  test("节点缺 node_id 时显示可见错误卡，而不是走 React key 警告或无限递归", async () => {
    const { tree } = await renderExplorer({
      nodes: [navigationNode("gw_1", "gw_1")],
      workspaces: [gatewayWorkspace("gw_1", "ready")],
      branchItems: [{ kind: "folder", name: "坏文件夹", has_children: true }],
    });
    await expandFirstWorkspace(tree);
    const html = JSON.stringify(tree.toJSON());
    expect(html).toContain("无法读取工作区目录");
    expect(html).toContain("node_id 必须是非空字符串");
  });

  test("同级节点重复 node_id 时显示可见错误卡", async () => {
    const { tree } = await renderExplorer({
      nodes: [navigationNode("gw_1", "gw_1")],
      workspaces: [gatewayWorkspace("gw_1", "ready")],
      branchItems: [
        { node_id: "dup", kind: "session", name: "甲", session_id: "s-a", has_children: false },
        { node_id: "dup", kind: "session", name: "乙", session_id: "s-b", has_children: false },
      ],
    });
    await expandFirstWorkspace(tree);
    const html = JSON.stringify(tree.toJSON());
    expect(html).toContain("无法读取工作区目录");
    expect(html).toContain("重复的 node_id: dup");
  });
});

describe("定位当前会话失败的错误归属", () => {
  test("导航加载成功但定位当前会话失败时，不得误报工作区列表不可用", async () => {
    const { html } = await renderExplorer({
      nodes: [navigationNode("gw_1", "正常的工作区")],
      workspaces: [gatewayWorkspace("gw_1", "ready")],
      activeWorkspaceId: "gw_1",
      currentSessionId: "s-hidden",
      breadcrumbFails: "面包屑读取炸了",
    });
    // 工作区导航本身读到了，仍应正常渲染工作区行。
    expect(html).toContain("正常的工作区");
    // 定位失败必须走独立错误卡，而不是宣称整个工作区列表不可用。
    expect(html).not.toContain("无法加载工作区列表");
    expect(html).toContain("无法定位当前会话");
    expect(html).toContain("面包屑读取炸了");
  });
});

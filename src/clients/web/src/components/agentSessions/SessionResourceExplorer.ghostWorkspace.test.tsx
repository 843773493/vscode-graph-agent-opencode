import { describe, expect, mock, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { GatewayWorkspace } from "../../types/backend";
import SessionResourceExplorer from "./SessionResourceExplorer";

// SessionResourceExplorer 的渲染依赖 explorer hook 与浮层；测试环境没有 DOM，
// 用最小替身固定「工作区导航树如何渲染」这一契约。
mock.module("../../hooks/session/useSessionResourceExplorer", () => ({
  useSessionResourceExplorer: () => explorerStub,
}));
mock.module("./SessionResourceOverlays", () => ({
  default: () => null,
}));

const explorerStub = {
  navigation: { nodes: [] as unknown[] } as { nodes: unknown[] } | null,
  navigationError: null as string | null,
  branches: new Map<string, unknown>(),
  expandedIds: new Set<string>(),
  searchResults: { items: [], workspaces: [] },
  searching: false,
  searchError: null,
  refreshNavigation: async () => undefined,
  refreshResourceTree: async () => undefined,
  loadBranch: async () => undefined,
  toggleExpanded: () => undefined,
  placeWorkspaceNode: async () => undefined,
  moveCatalogNode: async () => undefined,
};

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

function gatewayWorkspace(workspaceId: string, status: "ready" | "offline"): GatewayWorkspace {
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
    connection_error: null,
    checked_at: "2026-07-31T00:00:00Z",
  };
}

function renderExplorer(options: {
  nodes: unknown[];
  workspaces: GatewayWorkspace[];
  navigation?: { nodes: unknown[] } | null;
  navigationError?: string | null;
  branches?: Map<string, unknown>;
  expandedIds?: Set<string>;
}): string {
  explorerStub.navigation = options.navigation === undefined
    ? { nodes: options.nodes }
    : options.navigation;
  explorerStub.navigationError = options.navigationError ?? null;
  explorerStub.branches = options.branches ?? new Map<string, unknown>();
  explorerStub.expandedIds = options.expandedIds ?? new Set<string>();
  return renderToStaticMarkup(
    <SessionResourceExplorer
      apiPort={8014}
      workspaces={options.workspaces}
      activeWorkspaceId={null}
      currentSessionId=""
      searchOpen={false}
      searchQuery=""
      workspaceSwitching={false}
      startingWorkspaceIds={new Set()}
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
      generatorResources={{} as never}
    />,
  );
}

describe("工作区导航树对已消失工作区的渲染", () => {
  test("导航索引残留但工作区已不在列表时明确标记不可用并禁用激活", () => {
    const html = renderExplorer({
      nodes: [navigationNode("gw_gone", "已删除的工作区")],
      workspaces: [],
    });
    expect(html).toContain("已不可用");
    expect(html).toContain("已删除的工作区");
    // 激活按钮必须被禁用，避免点到已不存在的 workspace_id
    expect(html).toContain('disabled=""');
  });

  test("工作区仍在列表且就绪时不显示不可用提示", () => {
    const html = renderExplorer({
      nodes: [navigationNode("gw_ok", "正常的工作区")],
      workspaces: [gatewayWorkspace("gw_ok", "ready")],
    });
    expect(html).not.toContain("已不可用");
    expect(html).toContain("正常的工作区");
  });
});

describe("工作区导航树的空态与加载态", () => {
  test("导航已加载但没有任何工作区时给出明确空态而不是空树", () => {
    const html = renderExplorer({
      nodes: [],
      navigation: { nodes: [] },
      workspaces: [],
    });
    expect(html).toContain("还没有工作区");
    expect(html).toContain("添加工作区");
    expect(html).not.toContain("正在加载工作区目录");
  });

  test("导航尚未加载时显示加载态而不是空态", () => {
    const html = renderExplorer({
      nodes: [],
      navigation: null,
      workspaces: [],
    });
    expect(html).toContain("正在加载工作区目录");
    expect(html).not.toContain("还没有工作区");
  });

  test("导航加载失败时显示错误卡而不是空态", () => {
    const html = renderExplorer({
      nodes: [],
      navigation: null,
      navigationError: "HTTP 500 内部错误",
      workspaces: [],
    });
    expect(html).toContain("无法加载工作区列表");
    expect(html).toContain("HTTP 500 内部错误");
    expect(html).not.toContain("还没有工作区");
  });

  test("刷新失败但保留了空的旧导航快照时只显示错误卡，不并列空态", () => {
    const html = renderExplorer({
      nodes: [],
      navigation: { nodes: [] },
      navigationError: "HTTP 500 内部错误",
      workspaces: [],
    });
    expect(html).toContain("无法加载工作区列表");
    // 旧快照为空 + 刷新失败：错误优先，空态必须让位
    expect(html).not.toContain("还没有工作区");
  });
});

describe("会话目录分支的加载失败与空态", () => {
  function branch(overrides: Record<string, unknown>): Map<string, unknown> {
    return new Map([["gw_1:root", {
      revision: "",
      parent_node_id: null,
      items: [],
      cursor: null,
      total: 0,
      consistency_warning: null,
      loading: false,
      error: null,
      ...overrides,
    }]]);
  }

  test("分支加载失败时显示错误卡并保留技术详情", () => {
    const html = renderExplorer({
      nodes: [navigationNode("gw_1", "gw_1")],
      workspaces: [gatewayWorkspace("gw_1", "ready")],
      expandedIds: new Set(["workspace:gw_1"]),
      branches: branch({ error: "HTTP 500 内部错误" }),
    });
    expect(html).toContain("无法读取工作区目录");
    expect(html).toContain("HTTP 500 内部错误");
    // 失败时不得同时宣称「暂无会话」，否则用户无法区分空目录和加载失败
    expect(html).not.toContain("暂无会话或会话文件夹");
  });

  test("分支为空且无错时才显示暂无会话", () => {
    const html = renderExplorer({
      nodes: [navigationNode("gw_1", "gw_1")],
      workspaces: [gatewayWorkspace("gw_1", "ready")],
      expandedIds: new Set(["workspace:gw_1"]),
      branches: branch({}),
    });
    expect(html).toContain("暂无会话或会话文件夹");
    expect(html).not.toContain("无法读取工作区目录");
  });
});

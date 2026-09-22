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
  navigation: { nodes: [] as unknown[] },
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
}): string {
  explorerStub.navigation = { nodes: options.nodes };
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


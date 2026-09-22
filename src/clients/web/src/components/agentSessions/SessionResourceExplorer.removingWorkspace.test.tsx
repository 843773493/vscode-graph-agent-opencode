import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import {
  act,
  create,
  type ReactTestInstance,
  type ReactTestRenderer,
} from "react-test-renderer";
import type { GatewayWorkspace, WorkspaceNavigationNode } from "../../types/backend";
import type { SessionGeneratorResourcesController } from "../../hooks/sessionResourceExplorer/useSessionGeneratorResources";
import {
  apiResponse,
  installGatewayFetch,
  installTestWindow,
  restoreSessionHookGlobals,
} from "../../hooks/session/sessionHookTestFixtures";
import SessionResourceExplorer from "./SessionResourceExplorer";

// 工作区删除期间（removingGatewayWorkspaceIds 已写入、删除请求在途）这一行的契约：
// 必须先给出可见的「正在删除」状态，并且立刻失去激活、拖拽与右键菜单能力；
// 否则用户可以对一个正在消失的工作区继续发起操作。
// 用真实 useSessionResourceExplorer + fetch 桩，不替换 hook 模块。

afterEach(restoreSessionHookGlobals);

const generatorResources = {
  generators: null,
  generationRuns: new Map(),
  generatorError: null,
} as unknown as SessionGeneratorResourcesController;

function gatewayWorkspace(workspaceId: string): GatewayWorkspace {
  return {
    workspace_id: workspaceId,
    parent_workspace_id: null,
    name: workspaceId,
    root_path: "/home/test",
    backend_url: "http://127.0.0.1:9000",
    connection_kind: "local",
    status: "ready",
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

function navigationNode(workspaceId: string, name: string): WorkspaceNavigationNode {
  return {
    node_id: "gwn_" + workspaceId,
    kind: "workspace_ref",
    name,
    parent_node_id: null,
    workspace_id: workspaceId,
    position: 0,
    parent_path: null,
  } as unknown as WorkspaceNavigationNode;
}

async function mountExplorer(options: {
  workspaceId: string;
  removingWorkspaceIds: ReadonlySet<string>;
  onOpenWorkspaceMenu: (workspace: GatewayWorkspace, x: number, y: number) => void;
}): Promise<ReactTestRenderer> {
  installTestWindow(8014);
  installGatewayFetch((request) => {
    if (request.path.includes("/api/gateway/workspace-navigation")) {
      return apiResponse({
        revision: "navigation",
        nodes: [navigationNode(options.workspaceId, "目标工作区")],
      });
    }
    if (request.path.includes("/api/v1/session-catalog/children")) {
      return apiResponse({
        revision: "catalog",
        parent_node_id: null,
        items: [],
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
        workspaces={[gatewayWorkspace(options.workspaceId)]}
        activeWorkspaceId={null}
        currentSessionId=""
        searchOpen={false}
        searchQuery=""
        workspaceSwitching={false}
        startingWorkspaceIds={new Set()}
        removingWorkspaceIds={options.removingWorkspaceIds}
        onActivateWorkspace={async () => undefined}
        onSetWorkspaceParent={async () => undefined}
        onRefreshWorkspaceSessions={async () => undefined}
        onCreateSessionInFolder={async () => undefined}
        onSessionFolderDeleted={async () => undefined}
        catalogSyncKeys={new Map()}
        catalogRefreshVersions={new Map()}
        onSelectSession={() => undefined}
        onStatusChange={() => undefined}
        onOpenWorkspaceMenu={options.onOpenWorkspaceMenu}
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
  return tree;
}

function workspaceRow(tree: ReactTestRenderer): ReactTestInstance {
  const rows = tree.root.findAll(
    (instance) =>
      typeof instance.props.className === "string"
      && instance.props.className.includes("session-resource-row")
      && instance.props.className.includes("workspace"),
    { deep: true },
  );
  expect(rows.length).toBe(1);
  return rows[0];
}

function activateButton(row: ReactTestInstance): ReactTestInstance {
  const buttons = row.findAll(
    (instance) =>
      typeof instance.props.className === "string"
      && instance.props.className.includes("workspace-label"),
    { deep: true },
  );
  expect(buttons.length).toBe(1);
  return buttons[0];
}

describe("删除中的工作区在资源树里必须可见且不可交互", () => {
  test("删除中：显示正在删除，禁用激活与拖拽，右键不再打开工作区菜单", async () => {
    let menuCalls = 0;
    const tree = await mountExplorer({
      workspaceId: "gw_removing",
      removingWorkspaceIds: new Set(["gw_removing"]),
      onOpenWorkspaceMenu: () => {
        menuCalls += 1;
      },
    });
    const row = workspaceRow(tree);

    expect(JSON.stringify(tree.toJSON())).toContain("正在删除");
    expect(row.props.className).toContain("removing");
    expect(row.props.draggable).toBe(false);
    expect(activateButton(row).props.disabled).toBe(true);
    expect(activateButton(row).props.disabled).not.toBe(false);

    await act(async () => {
      row.props.onContextMenu({
        preventDefault: () => undefined,
        stopPropagation: () => undefined,
        clientX: 5,
        clientY: 6,
      });
    });
    expect(menuCalls).toBe(0);
  });

  test("未删除：仍可激活与拖拽，右键仍打开工作区菜单（对照，保证上面的禁用不是无差别禁用）", async () => {
    let menuCalls = 0;
    const tree = await mountExplorer({
      workspaceId: "gw_ready",
      removingWorkspaceIds: new Set<string>(),
      onOpenWorkspaceMenu: () => {
        menuCalls += 1;
      },
    });
    const row = workspaceRow(tree);

    expect(JSON.stringify(tree.toJSON())).not.toContain("正在删除");
    expect(row.props.className).not.toContain("removing");
    expect(row.props.draggable).toBe(true);
    expect(activateButton(row).props.disabled).toBe(false);

    await act(async () => {
      row.props.onContextMenu({
        preventDefault: () => undefined,
        stopPropagation: () => undefined,
        clientX: 5,
        clientY: 6,
      });
    });
    expect(menuCalls).toBeGreaterThan(0);
  });
});

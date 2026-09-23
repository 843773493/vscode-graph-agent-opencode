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

afterEach(restoreSessionHookGlobals);

const generatorResources = {
  generators: null,
  generationRuns: new Map(),
  generatorError: null,
} as unknown as SessionGeneratorResourcesController;

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

/** 渲染导航树 + 一个契约被破坏的会话目录节点，展开工作区后返回渲染器。 */
async function renderWithBranchItem(
  branchItems: unknown[],
): Promise<ReactTestRenderer> {
  installTestWindow(80_141);
  installGatewayFetch((request) => {
    if (request.path.includes("/api/gateway/workspace-navigation")) {
      return apiResponse({ revision: "navigation", nodes: [navigationNode("gw_1", "gw_1")] });
    }
    if (request.path.includes("/api/v1/session-catalog/children")) {
      return apiResponse({
        revision: "catalog",
        parent_node_id: null,
        items: branchItems,
        cursor: null,
        total: branchItems.length,
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
        apiPort={80_141}
        workspaces={[gatewayWorkspace("gw_1")]}
        activeWorkspaceId={null}
        currentSessionId=""
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
  return tree;
}

function catalogNodeRow(tree: ReactTestRenderer, nodeId: string) {
  return tree.root.findByProps({ "data-testid": `catalog-node-gw_1-${nodeId}` });
}

/** 右键菜单的可见失败出口：会话资源区的 actionError 横幅。 */
function actionErrorText(tree: ReactTestRenderer): string {
  const banners = tree.root.findAll(
    (instance) =>
      typeof instance.props.className === "string"
      && instance.props.className.split(" ").includes("session-resource-error"),
  );
  // 收集横幅子树内的全部文本节点，避免依赖具体层级结构。
  return banners
    .flatMap((banner) => banner.findAllByType("span"))
    .map((span) => span.props.children)
    .filter((child): child is string => typeof child === "string")
    .join(" ");
}

describe("会话目录节点右键菜单的契约破坏", () => {
  test("文件夹节点缺 folder_id 时响亮报错，而不是右键静默无反应", async () => {
    const tree = await renderWithBranchItem([
      { node_id: "fld_broken", kind: "folder", name: "坏文件夹", has_children: true },
    ]);
    expect(actionErrorText(tree)).toBe("");

    act(() => {
      catalogNodeRow(tree, "fld_broken").props.onContextMenu({
        preventDefault() {},
        stopPropagation() {},
        clientX: 10,
        clientY: 10,
      });
    });

    expect(actionErrorText(tree)).toContain("folder_id");
  });

  test("会话节点缺 session_id 时响亮报错，而不是右键静默无反应", async () => {
    const tree = await renderWithBranchItem([
      { node_id: "ses_broken", kind: "session", name: "坏会话", has_children: false },
    ]);

    act(() => {
      catalogNodeRow(tree, "ses_broken").props.onContextMenu({
        preventDefault() {},
        stopPropagation() {},
        clientX: 10,
        clientY: 10,
      });
    });

    expect(actionErrorText(tree)).toContain("会话 ID");
  });
});

import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { GatewayWorkspace } from "../../types/backend";
import {
  apiSuccessResponse,
  installGatewayFetch,
  restoreGatewayTestEnvironment,
} from "../../tests/fixtures/session/gatewayEnvironment";
import { installGatewayTestWindow } from "../../tests/fixtures/session/gatewayBrowserGlobals";
import SessionResourceExplorer from "./SessionResourceExplorer";

afterEach(restoreGatewayTestEnvironment);


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
    backend_workspace_id: "00000000-0000-0000-0000-000000000001",
    parent_workspace_id: null,
    name: workspaceId,
    backend_url: "http://127.0.0.1:9000",
    connection_kind: "local",
    status: "ready",
    active: false,
    managed: true,
    affected_workspace_ids: [],
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
  onOpenDiagnostic: (title: string, detail: string) => void,
): Promise<ReactTestRenderer> {
  installGatewayTestWindow(80_141);
  installGatewayFetch((request) => {
    if (request.path.includes("/api/gateway/workspace-navigation")) {
      return apiSuccessResponse({ revision: "navigation", nodes: [navigationNode("gw_1", "gw_1")] });
    }
    if (request.path.includes("/api/v1/session-catalog/children")) {
      return apiSuccessResponse({
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
        onOpenDiagnostic={onOpenDiagnostic}
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
  // 操作失败保留简短标题，技术诊断通过输出入口查看。
  return banners
    .flatMap((banner) => banner.findAllByType("strong"))
    .map((span) => span.props.children)
    .filter((child): child is string => typeof child === "string")
    .join(" ");
}

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

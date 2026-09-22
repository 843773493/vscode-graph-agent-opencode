import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { SessionGeneratorResourcesController } from "../../hooks/sessionResourceExplorer/useSessionGeneratorResources";
import type { GatewayWorkspace } from "../../types/backend";
import {
  apiResponse,
  installGatewayFetch,
  installTestWindow,
  restoreSessionHookGlobals,
} from "../../hooks/session/sessionHookTestFixtures";
import SessionGeneratorManager from "./SessionGeneratorManager";

afterEach(restoreSessionHookGlobals);

function workspace(workspaceId: string): GatewayWorkspace {
  return {
    workspace_id: workspaceId,
    parent_workspace_id: null,
    name: workspaceId,
    root_path: "/home/test",
    backend_url: "http://127.0.0.1:9000",
    connection_kind: "local",
    status: "ready",
    active: true,
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

const generatorResources = {
  generators: { revision: "r1", items: [] },
  generationRuns: new Map(),
  generatorError: null,
} as unknown as SessionGeneratorResourcesController;

function renderManager(): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <SessionGeneratorManager
        apiPort={8014}
        generatorResources={generatorResources}
        workspaces={[workspace("ws_folder")]}
        activeWorkspaceId="ws_folder"
        currentSessionId="ses_folder"
        onStatusChange={() => {}}
        onOpenConnectionManager={() => {}}
        onReconnectWorkspace={async () => {}}
        onStartWorkspace={async () => {}}
      />,
    );
  });
  return renderer;
}

function clickCreate(renderer: ReactTestRenderer): void {
  act(() => {
    renderer.root.findByProps({ className: "session-generator-create" }).props.onClick();
  });
}

function clickSessionFolderPlacement(renderer: ReactTestRenderer): void {
  const select = renderer.root.findByProps({
    value: "workspace",
  });
  act(() => {
    select.props.onChange({ target: { value: "session_folder" } });
  });
}

describe("SessionGeneratorManager 会话文件夹候选", () => {
  test("读取失败时必须显示可见错误，而不是静默当作没有文件夹", async () => {
    installTestWindow(8014);
    installGatewayFetch((request) => {
      if (request.path.includes("/api/v1/session-catalog/children")) {
        return new Response(JSON.stringify({ detail: "会话目录索引异常" }), {
          status: 500,
          headers: { "content-type": "application/json" },
        });
      }
      return apiResponse({ revision: "navigation", nodes: [] });
    }, { token: "generator-folder-token" });

    const renderer = renderManager();
    clickCreate(renderer);
    clickSessionFolderPlacement(renderer);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const alert = renderer.root.findAll(
      (node) => node.props.role === "alert"
        && typeof node.props.className === "string"
        && node.props.className.includes("session-resource-error"),
    );
    expect(alert.length).toBeGreaterThan(0);
    act(() => renderer.unmount());
  });
});

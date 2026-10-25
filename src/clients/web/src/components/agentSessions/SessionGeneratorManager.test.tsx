import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { SessionGeneratorResourcesController } from "../../hooks/sessionResourceExplorer/useSessionGeneratorResources";
import type {
  GatewayWorkspace,
  SessionGeneratorDefinition,
} from "../../types/backend";
import {
  apiSuccessResponse,
  installGatewayFetch,
  restoreGatewayTestEnvironment,
} from "../../tests/fixtures/session/gatewayEnvironment";
import { installGatewayTestWindow } from "../../tests/fixtures/session/gatewayBrowserGlobals";
import SessionGeneratorManager from "./SessionGeneratorManager";

afterEach(restoreGatewayTestEnvironment);

function workspace(workspaceId: string): GatewayWorkspace {
  return {
    workspace_id: workspaceId,
    backend_workspace_id: "00000000-0000-0000-0000-000000000001",
    parent_workspace_id: null,
    name: workspaceId,
    backend_url: "http://127.0.0.1:9000",
    connection_kind: "local",
    status: "ready",
    active: true,
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

function sessionGenerator(
  generatorId: string,
  workspaceId: string,
  status: "ready" | "blocked",
): SessionGeneratorDefinition {
  return {
    generator_id: generatorId,
    name: generatorId,
    status,
    status_reason: null,
    placement: { workspace_id: workspaceId },
    session_strategy: { mode: "new_per_run" },
    trigger: { type: "manual", timezone: "UTC" },
  } as unknown as SessionGeneratorDefinition;
}

function sessionGeneratorResources(
  items: SessionGeneratorDefinition[] = [],
): SessionGeneratorResourcesController {
  return {
    generators: { revision: "r1", items },
    generationRuns: new Map(),
    generatorError: null,
  } as unknown as SessionGeneratorResourcesController;
}

function renderManager(
  resources: SessionGeneratorResourcesController = sessionGeneratorResources(),
): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <SessionGeneratorManager
        apiPort={8014}
        generatorResources={resources}
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
    installGatewayTestWindow(8014);
    installGatewayFetch((request) => {
      if (request.path.includes("/api/v1/session-catalog/children")) {
        return new Response(JSON.stringify({ detail: "会话目录索引异常" }), {
          status: 500,
          headers: { "content-type": "application/json" },
        });
      }
      return apiSuccessResponse({ revision: "navigation", nodes: [] });
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

  test("摘要只统计当前工作区并显示各状态计数", () => {
    const renderer = renderManager(sessionGeneratorResources([
      sessionGenerator("当前正常", "ws_folder", "ready"),
      sessionGenerator("当前阻塞", "ws_folder", "blocked"),
      sessionGenerator("其他工作区", "ws_other", "ready"),
    ]));

    const summary = renderer.root.findByProps({ className: "session-automation-summary" });
    const summaryLabels = summary.findAllByType("span")
      .map((label) => label.children.join(""));
    const rows = renderer.root.findAllByProps({ className: "session-generator-row" });

    expect(summaryLabels).toEqual(["2 个自动化", "1 个正常", "1 个需要处理"]);
    expect(rows).toHaveLength(2);
    expect(summary.findByProps({ className: "attention" }).children.join(""))
      .toBe("1 个需要处理");
    act(() => renderer.unmount());
  });
});

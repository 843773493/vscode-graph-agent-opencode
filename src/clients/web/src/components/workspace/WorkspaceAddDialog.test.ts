import { describe, expect, test } from "bun:test";

import type { GatewayWorkspace } from "../../types/backend";
import { gatewayChoices } from "./WorkspaceAddDialog";

function createWorkspace(
  overrides: Partial<GatewayWorkspace>,
): GatewayWorkspace {
  return {
    workspace_id: "workspace-1",
    name: "workspace",
    root_path: "/workspace",
    backend_url: "http://127.0.0.1:8010",
    connection_kind: "local",
    status: "ready",
    active: false,
    managed: true,
    removable: true,
    system_default: false,
    remote: null,
    services: {},
    checked_at: "2026-07-30T00:00:00Z",
    ...overrides,
  };
}

describe("工作区设备选项", () => {
  test("本机使用用户可理解的名称和可用状态", () => {
    const [local] = gatewayChoices([createWorkspace({})]);

    expect(local).toMatchObject({
      kind: "local",
      name: "当前电脑",
      detail: "当前电脑",
      workspaceCount: 1,
      statusLabel: "可用",
    });
  });

  test("远程设备优先使用 SSH 别名并始终展示登录地址", () => {
    const choices = gatewayChoices([
      createWorkspace({
        connection_kind: "remote_gateway",
        remote: {
          gateway_connection_id: "connection-1",
          remote_workspace_id: "remote-workspace-1",
          gateway_id: "gateway-1",
          name: "127.0.0.1",
          host: "127.0.0.1",
          port: 22222,
          username: "boxteam",
          ssh_config_host: "boxteam-gateway-e2e",
          remote_gateway_port: 8014,
        },
      }),
    ]);

    expect(choices[1]).toMatchObject({
      kind: "remote",
      name: "boxteam-gateway-e2e",
      detail: "boxteam@127.0.0.1:22222",
      statusLabel: "SSH 已连接",
    });
  });

  test("没有别名和自定义名时使用 user@host，异常信息不会被吞掉", () => {
    const choices = gatewayChoices([
      createWorkspace({
        connection_kind: "remote_gateway",
        connection_error: "SSH 握手超时",
        remote: {
          gateway_connection_id: "connection-2",
          remote_workspace_id: "remote-workspace-2",
          gateway_id: "gateway-2",
          name: "10.0.0.8",
          host: "10.0.0.8",
          port: 22,
          username: "dev",
          remote_gateway_port: 8014,
        },
      }),
    ]);

    expect(choices[1]).toMatchObject({
      name: "dev@10.0.0.8",
      detail: "dev@10.0.0.8:22",
      status: "offline",
      statusLabel: "SSH 连接异常",
      error: "SSH 握手超时",
    });
  });
});

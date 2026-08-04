import { describe, expect, test } from "bun:test";

import type { GatewayWorkspace } from "../../types/backend";
import {
  workspaceFailurePresentation,
  workspaceStatusPresentation,
} from "./agentSessionsUtils";

function workspace(overrides: Partial<GatewayWorkspace>): GatewayWorkspace {
  return {
    workspace_id: "gw_test",
    parent_workspace_id: null,
    name: "home",
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
    ...overrides,
  };
}

describe("工作区故障展示", () => {
  test("远程异常转换为用户摘要，同时保留折叠技术详情", () => {
    const remote = workspace({
      connection_kind: "remote_gateway",
      status: "offline",
      connection_error: "LookupError: 远程 Gateway 隧道尚未连接: rgw_secret",
      remote: {
        gateway_connection_id: "rgw_secret",
        remote_workspace_id: "gw_remote",
        gateway_id: "gateway_remote",
        name: "GPU Gateway",
        host: "10.0.0.8",
        port: 22,
        username: "user",
        ssh_config_host: "gpu-dev",
        remote_gateway_port: 8014,
      },
    });

    expect(workspaceFailurePresentation(remote, remote.connection_error!)).toEqual({
      title: "无法读取远程工作区",
      message: "远程 Gateway“gpu-dev”当前未连接或暂时不可用。",
      technicalDetail: remote.connection_error!,
    });
    expect(workspaceStatusPresentation(remote)).toEqual({
      label: "连接失败",
      title: `远程 Gateway“gpu-dev”当前未连接。\n技术详情：${remote.connection_error}`,
    });
  });

  test("本地已停止工作区显示明确状态而不是无语义红点", () => {
    const local = workspace({ status: "offline" });

    expect(workspaceStatusPresentation(local)).toEqual({
      label: "已停止",
      title: "工作区后端未运行，可通过右键菜单重新启动。",
    });
  });
});

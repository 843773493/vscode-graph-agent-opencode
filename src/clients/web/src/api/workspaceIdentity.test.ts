import { describe, expect, test } from "bun:test";
import { gatewayWorkspaceRequestHeaders } from "./http";
import { requireGatewayWorkspaceRequestTarget } from "./workspaceIdentity";

const localWorkspace = {
  workspace_id: "gateway-local",
  backend_workspace_id: "00000000-0000-4000-8000-000000000001",
  connection_kind: "local" as const,
};

describe("Gateway workspace 请求身份", () => {
  test("local workspace 同时携带 Gateway target 与 backend UUID", () => {
    const target = requireGatewayWorkspaceRequestTarget(
      [localWorkspace],
      localWorkspace.workspace_id,
    );

    expect(gatewayWorkspaceRequestHeaders(target)).toEqual({
      "X-BoxTeam-Workspace-Id": "gateway-local",
      "X-BoxTeam-Backend-Workspace-Id": localWorkspace.backend_workspace_id,
    });
  });

  test("Gateway target 缺失或 local backend UUID 缺失时响亮失败", () => {
    expect(() => requireGatewayWorkspaceRequestTarget([], "gateway-local"))
      .toThrow("Gateway workspace target 不存在");
    expect(() => gatewayWorkspaceRequestHeaders({
      ...localWorkspace,
      backend_workspace_id: null,
    })).toThrow("缺少 backend_workspace_id");
  });

  test("remote gateway target 保留远端路由头，不伪造本地 backend UUID", () => {
    const target = {
      workspace_id: "gateway-remote",
      backend_workspace_id: null,
      connection_kind: "remote_gateway" as const,
    };

    expect(gatewayWorkspaceRequestHeaders(target)).toEqual({
      "X-BoxTeam-Workspace-Id": "gateway-remote",
    });
  });
});

import { describe, expect, test } from "bun:test";
import { gatewayWorkspaceRequestHeaders } from "./http";
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRoute, requireGatewayWorkspaceRequestTarget } from "./workspaceIdentity";

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

const owner = { workspaceId: localWorkspace.backend_workspace_id, gatewayId: "gateway_c" };
const remoteForkWorkspace = {
  workspace_id: "gateway-c-route",
  backend_workspace_id: null,
  connection_kind: "remote_gateway" as const,
  remote: { gateway_id: "gateway_c", remote_workspace_id: localWorkspace.backend_workspace_id },
};

describe("fork 发布结果的完整目标路由", () => {
  test("不同 Gateway 使用相同 backend UUID 时仍打开回执指定的远端", () => {
    const otherRemote = {
      ...remoteForkWorkspace, workspace_id: "gateway-b-route",
      remote: { ...remoteForkWorkspace.remote, gateway_id: "gateway_b" },
    };
    expect(requireGatewayWorkspaceRoute(
      [localWorkspace, otherRemote, remoteForkWorkspace], "gateway_a", owner,
    )).toBe(remoteForkWorkspace);
  });

  test("本地目标同时匹配实际 Gateway 身份和 backend UUID", () => {
    const localResult = { ...owner, gatewayId: "gateway_a" };
    expect(requireGatewayWorkspaceRoute(
      [remoteForkWorkspace, localWorkspace], "gateway_a", localResult,
    )).toBe(localWorkspace);
  });

  test("未知 Gateway 不借用同 UUID 的已登记工作区", () => {
    const unknown = { ...owner, gatewayId: "gateway_unknown" };
    expect(() => requireGatewayWorkspaceRoute([localWorkspace, remoteForkWorkspace], "gateway_a", unknown))
      .toThrow("缺少唯一的已登记路由");
  });

  test.each(["", " "])("回执缺少目标 Gateway 身份时不补默认路由：%s", (gatewayId) => {
    expect(() => requireGatewayWorkspaceRoute([remoteForkWorkspace], "gateway_a", { ...owner, gatewayId }))
      .toThrow("资源目标缺少 Gateway／工作区身份");
  });

  test("发布的目标工作区未登记时不借用其它同 Gateway 路由", () => {
    expect(() => requireGatewayWorkspaceRoute([remoteForkWorkspace], "gateway_a", {
      ...owner, workspaceId: "other-workspace",
    })).toThrow("缺少唯一的已登记路由");
  });

  test("相同完整目标存在重复登记时明确失败", () => {
    expect(() => requireGatewayWorkspaceRoute(
      [remoteForkWorkspace, { ...remoteForkWorkspace, workspace_id: "duplicate-route" }], "gateway_a", owner,
    )).toThrow("缺少唯一的已登记路由");
  });

  test("Gateway 身份缺失时不猜本地目标", () => {
    expect(() => requireGatewayWorkspaceRoute([remoteForkWorkspace], null, owner))
      .toThrow("健康回执缺少身份");
  });
});


describe("稳定工作区身份与临时路由", () => {
  test("本地和远端登记保留真正所属的 Gateway 与工作区", () => {
    expect(gatewayWorkspaceOwner(localWorkspace, "gateway_a")).toEqual({ gatewayId: "gateway_a", workspaceId: owner.workspaceId });
    expect(gatewayWorkspaceOwner(remoteForkWorkspace, "gateway_a")).toEqual(owner);
    expect(() => gatewayWorkspaceOwner({ ...remoteForkWorkspace, remote: null }, "gateway_a"))
      .toThrow("缺少目标 Gateway／工作区身份");
  });

  test("路由改变并被其它工作区复用后仍按稳定身份解析", () => {
    const changed = { ...remoteForkWorkspace, workspace_id: "new-route" };
    const reused = { ...remoteForkWorkspace, remote: { ...remoteForkWorkspace.remote, remote_workspace_id: "other-workspace" } };
    expect(requireGatewayWorkspaceRoute([reused, changed], "gateway_a", owner)).toBe(changed);
    expect(() => requireGatewayWorkspaceRoute([reused], "gateway_a", owner)).toThrow("缺少唯一的已登记路由");
  });
});

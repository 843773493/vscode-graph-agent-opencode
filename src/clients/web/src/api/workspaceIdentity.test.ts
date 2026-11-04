import { describe, expect, test } from "bun:test";
import { gatewayWorkspaceRequestHeaders, workspaceHeader } from "./http";
import {
  gatewayWorkspaceKey,
  gatewayWorkspaceOwner,
  nullableGatewayWorkspaceKey,
  requireGatewayWorkspaceRequestTarget,
  workspaceOwnerFromKey,
} from "./workspaceIdentity";

const workspaceId = "00000000-0000-4000-8000-000000000001";
const localWorkspace = { gateway_id: "gateway_a", workspace_id: workspaceId };
const remoteWorkspace = { gateway_id: "gateway_c", workspace_id: workspaceId };

describe("Gateway 与 UUID 的唯一工作区身份", () => {
  test("同 UUID 在两个 Gateway 下分别选择和传输", () => {
    for (const target of [localWorkspace, remoteWorkspace]) {
      const key = gatewayWorkspaceKey(target);
      expect(requireGatewayWorkspaceRequestTarget([localWorkspace, remoteWorkspace], key)).toBe(target);
      expect(workspaceOwnerFromKey(key)).toEqual(gatewayWorkspaceOwner(target));
      expect(gatewayWorkspaceRequestHeaders(target)).toEqual({
        "X-BoxTeam-Target-Gateway-Id": target.gateway_id,
        "X-BoxTeam-Workspace-Id": workspaceId,
      });
      expect(workspaceHeader(key)).toEqual(gatewayWorkspaceRequestHeaders(target));
    }
    expect(gatewayWorkspaceKey(localWorkspace)).not.toBe(gatewayWorkspaceKey(remoteWorkspace));
  });

  test("发布回执直接按 owner 解析，无需本机健康身份或连接别名", () => {
    const owner = { gatewayId: "gateway_c", workspaceId };
    expect(requireGatewayWorkspaceRequestTarget([localWorkspace, remoteWorkspace], owner)).toBe(remoteWorkspace);
    expect(gatewayWorkspaceKey(owner)).toBe(gatewayWorkspaceKey(remoteWorkspace));
  });

  test("未知 Gateway 不借用同 UUID 的其他登记", () => {
    expect(() => requireGatewayWorkspaceRequestTarget([localWorkspace, remoteWorkspace], {
      gatewayId: "gateway_unknown", workspaceId,
    })).toThrow("缺少唯一已登记工作区");
  });

  test("未知 UUID 不借用同 Gateway 的其他登记", () => {
    expect(() => requireGatewayWorkspaceRequestTarget([remoteWorkspace], {
      gatewayId: remoteWorkspace.gateway_id, workspaceId: "00000000-0000-4000-8000-000000000002",
    })).toThrow("缺少唯一已登记工作区");
  });

  test("同一完整 owner 重复登记时明确失败", () => {
    expect(() => requireGatewayWorkspaceRequestTarget([remoteWorkspace, { ...remoteWorkspace }], gatewayWorkspaceKey(remoteWorkspace)))
      .toThrow("缺少唯一已登记工作区");
  });

  test.each(["", " "])("Gateway 身份缺失时不补当前 Gateway：%s", gatewayId => {
    expect(() => gatewayWorkspaceKey({ gatewayId, workspaceId })).toThrow("缺少完整");
    expect(() => gatewayWorkspaceRequestHeaders({ ...remoteWorkspace, gateway_id: gatewayId })).toThrow("缺少");
  });

  test.each(["gw_legacy", workspaceId, '["gateway_c"]', '["gateway_c",null]', '["", "uuid"]'])(
    "不把缺少 owner 的旧选择交给当前工作区：%s", key => {
      expect(() => workspaceHeader(key)).toThrow();
    },
  );

  test("连接集合与顺序不能改写已登记 owner", () => {
    const connected = { ...remoteWorkspace, remote_gateways: [{ connection_id: "second" }, { connection_id: "first" }] };
    expect(gatewayWorkspaceOwner(connected)).toEqual({ gatewayId: "gateway_c", workspaceId });
    expect(gatewayWorkspaceKey(connected)).toBe(gatewayWorkspaceKey(remoteWorkspace));
  });

  test("只有空选择不生成目标头", () => {
    expect(nullableGatewayWorkspaceKey(null)).toBeNull();
    expect(workspaceHeader(null)).toEqual({});
  });
});

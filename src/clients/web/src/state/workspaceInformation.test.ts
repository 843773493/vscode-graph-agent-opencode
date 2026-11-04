import { expect, test } from "bun:test";
import {
  buildWorkspaceInformationDump,
  extractWorkspaceSelectionFromClipboardText,
  formatWorkspaceInformationDump,
} from "./workspaceInformation";
import { gatewayWorkspaceKey } from "../api/workspaceIdentity";
import type { GatewayWorkspace } from "../types/backend";

const parentId = "00000000-0000-4000-8000-000000000001";
const childId = "00000000-0000-4000-8000-000000000002";
function workspace(workspaceId: string, parent: GatewayWorkspace | null = null, gatewayId = "gateway-a"): GatewayWorkspace {
  return {
    gateway_id: gatewayId, workspace_id: workspaceId,
    parent_workspace: parent ? { gateway_id: parent.gateway_id, workspace_id: parent.workspace_id } : null,
    name: workspaceId, backend_url: "http://127.0.0.1:8010", connection_kind: "local",
    status: "ready", active: false, managed: true, affected_workspace_ids: [], removable: true,
    system_default: false, runtime_action: "safe_restart_managed_backend", remote_gateways: [],
    services: {}, checked_at: "2026-07-17T00:00:00Z",
  };
}

test("工作区信息只按完整 owner 收集子节点，JSON 保留原生 UUID", () => {
  const parent = workspace(parentId);
  const otherParent = workspace(parentId, null, "gateway-b");
  const child = workspace(childId, parent);
  const otherChild = workspace(childId, otherParent, "gateway-b");
  const dump = buildWorkspaceInformationDump(parent, [parent, child, otherParent, otherChild]);
  expect(dump.schema_version).toBe(2);
  expect(dump.workspace).toMatchObject({ gateway_id: "gateway-a", workspace_id: parentId,
    child_workspaces: [{ gateway_id: "gateway-a", workspace_id: childId }], remote_gateways: [] });
  expect(extractWorkspaceSelectionFromClipboardText(formatWorkspaceInformationDump(dump))).toBe(gatewayWorkspaceKey(parent));
  expect(extractWorkspaceSelectionFromClipboardText(gatewayWorkspaceKey(otherParent))).toBe(gatewayWorkspaceKey(otherParent));
});

test.each([parentId, "gw_parent", '{"kind":"boxteam_workspace_information","workspace":{"workspace_id":"uuid"}}'])(
  "缺少 Gateway 的剪贴板内容不补当前工作区：%s", text => {
    expect(() => extractWorkspaceSelectionFromClipboardText(text)).toThrow("Gateway／UUID");
  },
);

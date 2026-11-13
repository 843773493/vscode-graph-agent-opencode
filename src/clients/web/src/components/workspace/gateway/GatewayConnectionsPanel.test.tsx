import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { AddSshGatewayWorkspaceRequest } from "../../../types/backend";
import type { GatewayManagementDirectoryDTO } from "../../../types/protocol_generated/boxteam/gateway/v2/public";
import { apiSuccessResponse, installGatewayFetch, restoreGatewayTestEnvironment } from "../../../tests/fixtures/session/gatewayEnvironment";
import WarmConfirmProvider from "../../shell/WarmConfirmProvider";
import GatewayConnectionsPanel from "./GatewayConnectionsPanel";

afterEach(restoreGatewayTestEnvironment);
const directory: GatewayManagementDirectoryDTO = { local_gateway_id: "gateway_local", items: [
  { gateway_id: "gateway_manual", name: "零成员手动连接", connection_kind: "remote_gateway", gateway_connection_id: "rgw_manual", source_owner: "manual", remote_gateway_port: 9014 },
  { gateway_id: "gateway_config", name: "配置连接", connection_kind: "remote_gateway", gateway_connection_id: "rgw_config", source_owner: "config", remote_gateway_port: 9914 },
] };
const noop = async () => undefined;
async function flush() { for (let index = 0; index < 12; index++) await Promise.resolve(); }
function button(renderer: ReactTestRenderer, label: string) {
  return renderer.root.findAllByType("button").find(item => item.children.join("") === label)!;
}

test("原目录显示零工作区连接，manual改名保留原connection和端口，config不走manual写入", async () => {
  let reads = 0, settings = 0;
  const writes: AddSshGatewayWorkspaceRequest[] = [];
  installGatewayFetch(({ path }) => {
    if (path === "/api/gateway/management/gateways") { reads++; return apiSuccessResponse(directory); }
    return undefined;
  });
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => {
      renderer = create(<WarmConfirmProvider><GatewayConnectionsPanel apiPort={8014} gatewayId="gateway_local" workspaces={[]}
        revision={0} onAddSsh={async payload => { writes.push(payload); }} onRefresh={noop}
        onRemoteGatewayReconnected={() => undefined} onOpenConfiguration={async () => { settings++; }} onOpenLogs={() => undefined} /></WarmConfirmProvider>);
      await flush();
    });
    expect(renderer.root.findAllByType("article")).toHaveLength(2);
    expect(renderer.root.findAllByType("button").filter(item => item.children.join("") === "移除连接")).toHaveLength(1);
    await act(async () => { button(renderer, "编辑显示名").props.onClick(); });
    await act(async () => { button(renderer, "取消").props.onClick(); });
    expect(writes).toHaveLength(0);
    await act(async () => { button(renderer, "编辑显示名").props.onClick(); });
    await act(async () => { renderer.root.findByType("input").props.onChange({ target: { value: "修改后" } }); });
    await act(async () => { renderer.root.findByType("form").props.onSubmit({ preventDefault() {} }); await flush(); });
    expect(writes).toEqual([{ connection_id: "rgw_manual", name: "修改后", remote_gateway_port: 9014 }]);
    expect(reads).toBe(2);
    await act(async () => { button(renderer, "打开 Gateway 来源设置").props.onClick(); await flush(); });
    expect(settings).toBe(1);
    expect(writes).toHaveLength(1);
  } finally { if (renderer) await act(async () => renderer.unmount()); }
});

test("目录HTTP失败保留request_id，不显示空连接成功状态", async () => {
  installGatewayFetch(({ path }) => path === "/api/gateway/management/gateways"
    ? Response.json({ detail: "目录不可读" }, { status: 503, headers: { "X-Request-ID": "req-directory-failed" } }) : undefined);
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => {
      renderer = create(<WarmConfirmProvider><GatewayConnectionsPanel apiPort={8014} gatewayId="gateway_local" workspaces={[]}
        revision={0} onAddSsh={noop} onRefresh={noop} onRemoteGatewayReconnected={() => undefined}
        onOpenConfiguration={noop} onOpenLogs={() => undefined} /></WarmConfirmProvider>);
      await flush();
    });
    expect(renderer.root.findByProps({ role: "alert" }).children.join("")).toContain("req-directory-failed");
    expect(JSON.stringify(renderer.toJSON())).not.toContain("暂无远程 Gateway 连接");
    expect(renderer.root.findAllByType("article")).toHaveLength(0);
  } finally { if (renderer) await act(async () => renderer.unmount()); }
});

import { renameGatewayWorkspace } from "../../gatewayApi";
import type { GatewayWorkspace } from "../../types/backend";
import { workspaceHoverTitle } from "../agentSessions/AgentSessionsWorkspaceGroups";
import { groupGatewayWorkspaces } from "./GatewayControlCenter";

const originalFetch = globalThis.fetch;
let requestUrl = "";
let requestInit: Parameters<typeof fetch>[1];

globalThis.fetch = Object.assign(
  async (...args: Parameters<typeof fetch>) => {
    const [input, init] = args;
    requestUrl = String(input);
    requestInit = init;
    if (
      new URL(requestUrl).pathname ===
      "/api/gateway/auth/local-credential"
    ) {
      return new Response(
        JSON.stringify({
          code: 0,
          message: "ok",
          request_id: "req_local_credential",
          data: { token: "test-local-token" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(
      JSON.stringify({
        code: 0,
        message: "ok",
        request_id: "req_test",
        data: { active_workspace_id: "gw_ssh", items: [] },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  },
  { preconnect: originalFetch.preconnect },
);

try {
  await renameGatewayWorkspace(8014, "gw/ssh", { name: "远程开发" });
  if (!requestUrl.endsWith("/api/gateway/workspaces/gw%2Fssh")) {
    throw new Error(`重命名接口路径错误: ${requestUrl}`);
  }
  if (requestInit?.method !== "PATCH") {
    throw new Error(`重命名接口方法错误: ${requestInit?.method}`);
  }
  if (requestInit?.body !== JSON.stringify({ name: "远程开发" })) {
    throw new Error(`重命名请求体错误: ${String(requestInit?.body)}`);
  }
} finally {
  globalThis.fetch = originalFetch;
}

const remoteGatewayWorkspace: GatewayWorkspace = {
  workspace_id: "gw_ssh",
  name: "远程开发",
  root_path: "/srv/project",
  backend_url: "http://127.0.0.1:42000",
  connection_kind: "remote_gateway",
  status: "ready",
  active: true,
  managed: true,
  removable: true,
  system_default: false,
  runtime_action: "reconnect_remote_gateway",
  config_reload: {
    available: true,
    healthy: true,
    restart_required: false,
    changed_sections: [],
  },
  remote: {
    gateway_connection_id: "remote_gateway_dev",
    gateway_id: "gateway_dev",
    remote_workspace_id: "workspace_dev",
    name: "开发服务器",
    host: "192.0.2.10",
    port: 22,
    username: "developer",
    ssh_config_host: "dev-server",
    remote_gateway_port: 8014,
  },
  services: {},
  checked_at: "2026-07-16T00:00:00Z",
};

const hoverTitle = workspaceHoverTitle(remoteGatewayWorkspace);
if (!hoverTitle.includes("路径：/srv/project")) {
  throw new Error(`悬停提示缺少完整路径: ${hoverTitle}`);
}
if (!hoverTitle.includes("远程 Gateway：gateway_dev")) {
  throw new Error(`悬停提示缺少远程 Gateway 信息: ${hoverTitle}`);
}

const grouped = groupGatewayWorkspaces([
  remoteGatewayWorkspace,
  {
    ...remoteGatewayWorkspace,
    workspace_id: "gw_ssh_second",
    name: "远程后端",
    root_path: "/srv/backend",
  },
]);
if (grouped.length !== 1 || grouped[0].workspaces.length !== 2) {
  throw new Error("同一远程 Gateway 的工作区没有归入同一连接分组");
}
if (grouped[0].title !== "dev-server") {
  throw new Error(`连接分组没有优先显示 SSH 别名: ${grouped[0].title}`);
}
if (!grouped[0].connectionLabel.includes("developer@192.0.2.10:22")) {
  throw new Error(`连接分组缺少 SSH Host: ${grouped[0].connectionLabel}`);
}

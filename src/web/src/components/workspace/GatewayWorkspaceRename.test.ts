import { renameGatewayWorkspace } from "../../gatewayApi";
import type { GatewayWorkspace } from "../../types/backend";
import { workspaceHoverTitle } from "../agentSessions/AgentSessionsWorkspaceGroups";

const originalFetch = globalThis.fetch;
let requestUrl = "";
let requestInit: Parameters<typeof fetch>[1];

globalThis.fetch = Object.assign(
  async (...args: Parameters<typeof fetch>) => {
    const [input, init] = args;
    requestUrl = String(input);
    requestInit = init;
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

const sshWorkspace: GatewayWorkspace = {
  workspace_id: "gw_ssh",
  name: "远程开发",
  root_path: "/srv/project",
  backend_url: "http://127.0.0.1:42000",
  connection_kind: "ssh",
  status: "ready",
  active: true,
  managed: true,
  removable: true,
  system_default: false,
  remote: {
    host: "dev.example.com",
    port: 2222,
    username: "alice",
  },
  services: {},
  checked_at: "2026-07-16T00:00:00Z",
};

const hoverTitle = workspaceHoverTitle(sshWorkspace);
if (!hoverTitle.includes("路径：/srv/project")) {
  throw new Error(`悬停提示缺少完整路径: ${hoverTitle}`);
}
if (!hoverTitle.includes("SSH：alice@dev.example.com:2222")) {
  throw new Error(`悬停提示缺少 SSH 主机信息: ${hoverTitle}`);
}

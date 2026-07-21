import {
  probeExternalGatewayWorkspace,
  reconnectGatewayWorkspace,
  forceRestartManagedGatewayWorkspaceBackend,
  safeRestartManagedGatewayWorkspaceBackend,
} from "../../gatewayApi";

const requestedPaths: string[] = [];
const originalFetch = globalThis.fetch;
globalThis.fetch = Object.assign(
  async (...args: Parameters<typeof fetch>) => {
    const [input, init] = args;
    const path = new URL(String(input)).pathname;
    if (path === "/api/gateway/auth/local-credential") {
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
    requestedPaths.push(path);
    if (init?.method !== "POST") {
      throw new Error(`运行时控制请求必须使用 POST: ${String(init?.method)}`);
    }
    return new Response(
      JSON.stringify({
        code: 0,
        message: "ok",
        request_id: "req_runtime_action",
        data: {
          workspace_id: "gw/test",
          status: "restarted",
          forced: false,
          blockers: [],
          workspaces: {
            active_workspace_id: "gw/test",
            items: [],
          },
        },
      }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      },
    );
  },
  { preconnect: originalFetch.preconnect },
);

try {
  await safeRestartManagedGatewayWorkspaceBackend(8014, "gw/test");
  await forceRestartManagedGatewayWorkspaceBackend(8014, "gw/test");
  await reconnectGatewayWorkspace(8014, "gw/test");
  await probeExternalGatewayWorkspace(8014, "gw/test");
} finally {
  globalThis.fetch = originalFetch;
}

const expectedPaths = [
  "/api/gateway/workspaces/gw%2Ftest/runtime/restart-safe",
  "/api/gateway/workspaces/gw%2Ftest/runtime/restart-force",
  "/api/gateway/workspaces/gw%2Ftest/reconnect",
  "/api/gateway/workspaces/gw%2Ftest/probe",
];
if (requestedPaths.join("\n") !== expectedPaths.join("\n")) {
  throw new Error(
    `Gateway 运行时控制路径错误:\n${requestedPaths.join("\n")}`,
  );
}

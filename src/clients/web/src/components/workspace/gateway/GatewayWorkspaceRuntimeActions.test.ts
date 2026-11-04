import { gatewayWorkspaceKey } from "../../../api/workspaceIdentity";
import {
  probeExternalGatewayWorkspace,
  reconnectGatewayWorkspace,
  forceRestartManagedGatewayWorkspaceBackend,
  restartDevelopmentRuntime,
  safeRestartManagedGatewayWorkspaceBackend,
  startManagedGatewayWorkspaceBackend,
  stopManagedGatewayWorkspaceBackend,
} from "../../../gatewayApi";

const WORKSPACE_ID = "00000000-0000-4000-8000-000000000001";
const SIBLING_ID = "00000000-0000-4000-8000-000000000002";
const WORKSPACE_KEY = gatewayWorkspaceKey({ gateway_id: "gateway-test", workspace_id: WORKSPACE_ID });

const requestedPaths: string[] = [];
const requestedBodies: unknown[] = [];
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
    if (path.startsWith("/api/gateway/workspaces/")) {
      const headers = new Headers(init?.headers);
      if (headers.get("X-BoxTeam-Workspace-Id") !== WORKSPACE_ID || headers.get("X-BoxTeam-Target-Gateway-Id") !== "gateway-test") {
        throw new Error("生命周期请求丢失原生 Gateway／UUID 归属");
      }
    }
    if (/\/runtime\/(restart-safe|restart-force|start|stop)$/.test(path)) {
      requestedBodies.push(JSON.parse(String(init?.body)));
    }
    if (init?.method !== "POST") {
      throw new Error(`运行时控制请求必须使用 POST: ${String(init?.method)}`);
    }
    return new Response(
      JSON.stringify({
        code: 0,
        message: "ok",
        request_id: "req_runtime_action",
        data: {
          gateway_id: "gateway-test", workspace_id: WORKSPACE_ID,
          status: path.endsWith("/start") ? "started" : path.endsWith("/stop") ? "stopped" : "restarted",
          forced: path.endsWith("/restart-force"),
          backend_generation: "generation-2",
          affected_workspace_ids: [WORKSPACE_ID, SIBLING_ID],
          blockers: [],
          workspaces: {
            active_gateway_id: "gateway-test", workspace_id: WORKSPACE_ID,
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

const groupRequest = {
  expected_backend_generation: "generation-1",
  expected_workspace_ids: [WORKSPACE_ID, SIBLING_ID],
  desired_workspace_ids: [WORKSPACE_ID],
};
try {
  const safe = await safeRestartManagedGatewayWorkspaceBackend(8014, WORKSPACE_KEY, groupRequest);
  const force = await forceRestartManagedGatewayWorkspaceBackend(8014, WORKSPACE_KEY, groupRequest);
  const started = await startManagedGatewayWorkspaceBackend(8014, WORKSPACE_KEY, groupRequest);
  const stopped = await stopManagedGatewayWorkspaceBackend(8014, WORKSPACE_KEY, groupRequest);
  for (const result of [safe, force, started, stopped]) {
    if (result.backend_generation !== "generation-2"
      || result.affected_workspace_ids.join(",") !== `${WORKSPACE_ID},${SIBLING_ID}`) {
      throw new Error("完整 Backend generation/affected 响应丢失");
    }
  }
  if (!force.forced || safe.forced || started.status !== "started" || stopped.status !== "stopped") {
    throw new Error("完整 Backend 生命周期回执不匹配");
  }
  await reconnectGatewayWorkspace(8014, WORKSPACE_KEY);
  await probeExternalGatewayWorkspace(8014, WORKSPACE_KEY);
  await restartDevelopmentRuntime(8014);
} finally {
  globalThis.fetch = originalFetch;
}

const expectedPaths = [
  `/api/gateway/workspaces/${WORKSPACE_ID}/runtime/restart-safe`,
  `/api/gateway/workspaces/${WORKSPACE_ID}/runtime/restart-force`,
  `/api/gateway/workspaces/${WORKSPACE_ID}/runtime/start`,
  `/api/gateway/workspaces/${WORKSPACE_ID}/runtime/stop`,
  `/api/gateway/workspaces/${WORKSPACE_ID}/reconnect`,
  `/api/gateway/workspaces/${WORKSPACE_ID}/probe`,
  "/api/gateway/runtime/restart-development",
];
if (requestedPaths.join("\n") !== expectedPaths.join("\n")) {
  throw new Error(
    `Gateway 运行时控制路径错误:\n${requestedPaths.join("\n")}`,
  );
}

if (requestedBodies.length !== 4
  || requestedBodies.some((body) => JSON.stringify(body) !== JSON.stringify(groupRequest))) {
  throw new Error("完整 expected/desired 成员合同没有原样传入生命周期 JSON 请求");
}

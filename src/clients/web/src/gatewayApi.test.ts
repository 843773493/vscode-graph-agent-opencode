import { afterEach, describe, expect, test } from "bun:test";

import {
  addManagedGatewayWorkspace,
  browseGatewayLocalDirectories,
  createSessionConnection,
  ensureGatewayUserAccess,
  heartbeatGatewayUserWithRetry,
  listGatewayWorkspaces,
} from "./gatewayApi";
import { requestJson } from "./api";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("手动创建会话连接", () => {
  test("终端和浏览器复用工作区 manager，并绑定当前会话", async () => {
    const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
    let requestCount = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input, init] = args;
        requestCount += 1;
        if (requestCount === 1) {
          return Response.json({ data: { token: "test-token" } });
        }
        const url = String(input);
        requests.push({
          url,
          body: JSON.parse(String(init?.body)) as Record<string, unknown>,
        });
        return url.includes("terminal-manager")
          ? Response.json({ data: { terminal_id: "term_manual" } })
          : Response.json({ data: { browser_id: "browser_manual" } });
      },
      { preconnect: originalFetch.preconnect },
    );

    const terminal = await createSessionConnection(
      49_101,
      "gw_manual",
      "ses_manual",
      "terminal",
    );
    const browser = await createSessionConnection(
      49_101,
      "gw_manual",
      "ses_manual",
      "browser",
    );

    expect(terminal).toEqual({ kind: "terminal", resourceId: "term_manual" });
    expect(browser).toEqual({ kind: "browser", resourceId: "browser_manual" });
    expect(requests).toHaveLength(2);
    expect(requests[0].url).toContain(
      "/api/gateway/workspaces/gw_manual/terminal-manager/api/terminals",
    );
    expect(requests[0].body.session_id).toBe("ses_manual");
    expect(requests[1].url).toContain(
      "/api/gateway/workspaces/gw_manual/browser-manager/api/browsers",
    );
    expect(requests[1].body).toMatchObject({
      session_id: "ses_manual",
      url: "about:blank",
    });
  });
});

describe("Gateway 本机目录浏览", () => {
  test("开发代理瞬时返回 503 时重试一次目录读取", async () => {
    let requestCount = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        requestCount += 1;
        if (requestCount === 1) {
          return Response.json({
            data: { token: "directory-test-token" },
            request_id: "req_token",
          });
        }
        if (requestCount === 2) {
          return new Response(null, {
            status: 503,
            statusText: "Service Unavailable",
          });
        }
        return Response.json({
          data: {
            path: "/workspace",
            parent_path: "/",
            home_path: "/home/test",
            entries: [{ name: "project", path: "/workspace/project" }],
            truncated: false,
            limit: 120,
          },
          request_id: "req_directory",
        });
      },
      { preconnect: originalFetch.preconnect },
    );

    const listing = await browseGatewayLocalDirectories(49_102, "/workspace");

    expect(requestCount).toBe(3);
    expect(listing.entries).toEqual([
      { name: "project", path: "/workspace/project" },
    ]);
  });

  test("选择远程 Gateway 后把连接标识与目录一起发送", async () => {
    const requestedUrls: string[] = [];
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        requestedUrls.push(String(input));
        if (requestedUrls.length === 1) {
          return Response.json({ data: { token: "remote-directory-token" } });
        }
        return Response.json({
          data: {
            path: "/srv/projects",
            parent_path: "/srv",
            home_path: "/home/remote",
            entries: [],
            truncated: false,
            limit: 120,
          },
          request_id: "req_remote_directory",
        });
      },
      { preconnect: originalFetch.preconnect },
    );

    await browseGatewayLocalDirectories(
      49_103,
      "/srv/projects",
      "rgw_remote",
    );

    expect(requestedUrls[1]).toContain(
      "/api/gateway/local-directories?path=%2Fsrv%2Fprojects&gateway_connection_id=rgw_remote",
    );
  });
});

describe("Gateway 工作区注册", () => {
  test("注册到所选 Gateway 且默认不创建目录", async () => {
    const captured: { requestBody: Record<string, unknown> | null } = {
      requestBody: null,
    };
    let requestCount = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [, init] = args;
        requestCount += 1;
        if (requestCount === 1) {
          return Response.json({ data: { token: "managed-workspace-token" } });
        }
        captured.requestBody = JSON.parse(
          String(init?.body),
        ) as Record<string, unknown>;
        return Response.json({
          data: { gateway_connection_id: "rgw_remote", workspaces: [] },
          request_id: "req_managed_workspace",
        });
      },
      { preconnect: originalFetch.preconnect },
    );

    await addManagedGatewayWorkspace(49_104, {
      gateway_connection_id: "rgw_remote",
      root_path: "/srv/projects/alpha",
    });

    expect(captured.requestBody).toEqual({
      gateway_connection_id: "rgw_remote",
      root_path: "/srv/projects/alpha",
      create_directory: false,
    });
  });
});

describe("Gateway 工作区列表", () => {
  test("支持切换期间跳过全量健康探测", async () => {
    const requestedUrls: string[] = [];
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        requestedUrls.push(String(input));
        if (requestedUrls.length === 1) {
          return Response.json({ data: { token: "workspace-list-token" } });
        }
        return Response.json({
          data: { active_workspace_id: "gw_fast", items: [] },
          request_id: "req_workspace_list",
        });
      },
      { preconnect: originalFetch.preconnect },
    );

    await listGatewayWorkspaces(49_105, { checkHealth: false });

    expect(requestedUrls[1]).toContain(
      "/api/gateway/workspaces?check_health=false",
    );
  });
});

describe("Gateway 认证初始化", () => {
  test("业务请求必须等待 current 用户会话成功后再发送", async () => {
    const port = 49_108;
    const requestedPaths: string[] = [];
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input)).pathname;
        requestedPaths.push(path);
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            data: { token: "business-gate-token" },
            request_id: "req_business_gate_token",
          });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            data: {
              kind: "guest",
              user_id: null,
              lease_generation: 1,
              expires_at: null,
              takeover: false,
            },
            request_id: "req_business_gate_current",
          });
        }
        if (path === "/api/v1/workspace") {
          return Response.json({
            data: { workspace_id: "ws_gate" },
            request_id: "req_business_gate_workspace",
          });
        }
        throw new Error(`Unexpected request: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    await expect(requestJson<{ data: { workspace_id: string } }>(
      port,
      "/api/v1/workspace",
    )).resolves.toMatchObject({ data: { workspace_id: "ws_gate" } });
    expect(requestedPaths).toEqual([
      "/api/gateway/auth/local-credential",
      "/api/gateway/users/current",
      "/api/v1/workspace",
    ]);
  });

  test("React StrictMode 并发初始化只探测一次并只创建一个 guest", async () => {
    const port = 49_106;
    let credentialCalls = 0;
    let currentCalls = 0;
    let guestCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input)).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          credentialCalls += 1;
          return Response.json({
            data: { token: "auth-init-token" },
            request_id: "req_auth_init_token",
          });
        }
        if (path === "/api/gateway/users/current") {
          currentCalls += 1;
          return Response.json(
            { detail: "user_session_required" },
            { status: 401 },
          );
        }
        if (path === "/api/gateway/users/guest") {
          guestCalls += 1;
          return Response.json({
            data: {
              kind: "guest",
              user_id: null,
              lease_generation: 1,
              expires_at: null,
              takeover: false,
            },
            request_id: "req_auth_init_guest",
          });
        }
        throw new Error(`Unexpected request: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    const [first, second] = await Promise.all([
      ensureGatewayUserAccess(port),
      ensureGatewayUserAccess(port),
    ]);

    expect(first).toEqual(second);
    expect(credentialCalls).toBe(1);
    expect(currentCalls).toBe(1);
    expect(guestCalls).toBe(1);
  });

  test("heartbeat 只对网络传输失败做有界重试", async () => {
    const port = 49_107;
    let heartbeatCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0])).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            data: { token: "heartbeat-retry-token" },
            request_id: "req_heartbeat_retry_token",
          });
        }
        if (path === "/api/gateway/users/current/heartbeat") {
          heartbeatCalls += 1;
          if (heartbeatCalls === 1) {
            throw new TypeError("网络切换");
          }
          return Response.json({
            data: {
              kind: "guest",
              user_id: null,
              lease_generation: 1,
              expires_at: null,
              takeover: false,
            },
            request_id: "req_heartbeat_retry",
          });
        }
        throw new Error(`Unexpected request: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    await heartbeatGatewayUserWithRetry(port);

    expect(heartbeatCalls).toBe(2);
  });
});

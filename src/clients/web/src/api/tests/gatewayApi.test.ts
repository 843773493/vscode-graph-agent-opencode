import { afterEach, describe, expect, test } from "bun:test";

import {
  addManagedGatewayWorkspace,
  browseGatewayLocalDirectories,
  deleteGatewayUiAsset,
  getGatewayUiSettings,
  listGatewayUiAssets,
  listGatewayWorkspaces,
} from "../../gatewayApi";
import { createSessionConnection } from "../gateway/sessionConnections";
import {
  acquireGatewayGuest,
  createGatewayUser,
  deleteGatewayUser,
  ensureGatewayUserAccess,
  heartbeatGatewayUserWithRetry,
  listGatewayUsers,
  selectGatewayUser,
  takeoverGatewayUser,
} from "../gateway/userAccess";
import {
  getGatewayUserViewState,
  getLatestGatewayUserViewState,
} from "../gateway/userViewState";
import { requestJson } from "../../api";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

interface CapturedUserRequest {
  method: string;
  path: string;
  body: unknown;
}

/**
 * 安装一段记录用户访问控制请求的 fetch 桩：先应答屏障所需的本地凭据与
 * current 探测，再记录目标请求；所有响应都带 request_id 以满足解包契约。
 */
function stubUserAccessFetch(
  captured: CapturedUserRequest[],
  respond: () => unknown,
): void {
  globalThis.fetch = Object.assign(
    async (...args: Parameters<typeof fetch>) => {
      const [input, init] = args;
      const path = new URL(String(input), "http://127.0.0.1").pathname;
      if (path === "/api/gateway/auth/local-credential") {
        return Response.json({ data: { token: "user-access-token" } });
      }
      if (path === "/api/gateway/users/current") {
        return Response.json({
          data: { kind: "guest", user_id: null, lease_generation: 1 },
          request_id: "req_user_access_current",
        });
      }
      captured.push({
        method: init?.method ?? "GET",
        path,
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });
      return Response.json(
        { data: respond(), request_id: "req_user_access" },
      );
    },
    { preconnect: originalFetch.preconnect },
  );
}

describe("Gateway 用户访问控制请求", () => {
  test("删除用户使用 DELETE 且路径做 URL 编码", async () => {
    const captured: CapturedUserRequest[] = [];
    stubUserAccessFetch(captured, () => ({ user_id: "u 1" }));

    await deleteGatewayUser(49_910, "u 1");

    expect(captured).toEqual([
      { method: "DELETE", path: "/api/gateway/users/u%201", body: null },
    ]);
  });

  test("列表与创建用户携带正确的方法与请求体", async () => {
    const captured: CapturedUserRequest[] = [];
    stubUserAccessFetch(captured, () => ({ items: [] }));

    await listGatewayUsers(49_911);
    await createGatewayUser(49_911, { display_name: "新用户" });

    expect(captured).toEqual([
      { method: "GET", path: "/api/gateway/users", body: null },
      {
        method: "POST",
        path: "/api/gateway/users",
        body: { display_name: "新用户" },
      },
    ]);
  });

  test("select/takeover 分别落到 access 与 takeover 且透传 client_label", async () => {
    const captured: CapturedUserRequest[] = [];
    stubUserAccessFetch(captured, () => ({
      kind: "user",
      user_id: "usr_1",
      lease_generation: 1,
    }));

    await selectGatewayUser(49_912, "usr_1", "我的浏览器");
    await takeoverGatewayUser(49_912, "usr_1");

    expect(captured).toEqual([
      {
        method: "POST",
        path: "/api/gateway/users/usr_1/access",
        body: { client_label: "我的浏览器" },
      },
      {
        method: "POST",
        path: "/api/gateway/users/usr_1/takeover",
        body: { client_label: null },
      },
    ]);
  });

  test("显式切换游客直接 POST guest 且不发送额外字段", async () => {
    const captured: CapturedUserRequest[] = [];
    stubUserAccessFetch(captured, () => ({
      kind: "guest",
      user_id: null,
      lease_generation: 2,
    }));

    await acquireGatewayGuest(49_913);

    expect(captured).toEqual([
      { method: "POST", path: "/api/gateway/users/guest", body: {} },
    ]);
  });
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
      49_901,
      "gw_manual",
      "ses_manual",
      "terminal",
    );
    const browser = await createSessionConnection(
      49_901,
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

    const listing = await browseGatewayLocalDirectories(49_902, "/workspace");

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
      49_903,
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

    await addManagedGatewayWorkspace(49_904, {
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

    await listGatewayWorkspaces(49_905, { checkHealth: false });

    expect(requestedUrls[1]).toContain(
      "/api/gateway/workspaces?check_health=false",
    );
  });
});

describe("Gateway 认证初始化", () => {
  test("业务请求必须等待 current 用户会话成功后再发送", async () => {
    const port = 49_908;
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
    const port = 49_906;
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
    const port = 49_907;
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

describe("Gateway UI 资源列表载荷校验", () => {
  /** 安装带凭据与护栏应答的 fetch 桩，正文由 items 入参决定。 */
  function stubUiAssetsFetch(items: unknown): void {
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), "http://127.0.0.1").pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "ui-assets-token" } });
        }
        return Response.json({
          data: items === undefined ? {} : { items },
          request_id: "req_ui_assets",
        });
      },
      { preconnect: originalFetch.preconnect },
    );
  }

  test("合法资源数组正常返回", async () => {
    stubUiAssetsFetch([
      { asset_id: "asset_1", name: "主题", mime_type: "image/png" },
    ]);

    const assets = await listGatewayUiAssets(49_920);

    expect(assets).toHaveLength(1);
    expect(assets[0].asset_id).toBe("asset_1");
  });

  test("items 为 null / 对象 / 字符串时响亮失败，不伪造空列表", async () => {
    for (const items of [null, {}, "x"]) {
      stubUiAssetsFetch(items);
      const expected = items === null ? "null" : items === "x" ? "string" : "object";
      await expect(listGatewayUiAssets(49_921)).rejects.toThrow(
        `Gateway UI 资源列表响应 items 必须是数组，实际为 ${expected}`,
      );
    }
  });

  test("缺失 items 字段同样响亮失败", async () => {
    stubUiAssetsFetch(undefined);

    await expect(listGatewayUiAssets(49_922)).rejects.toThrow(
      "Gateway UI 资源列表响应 items 必须是数组，实际为 undefined",
    );
  });

  test("删除资源同样按数组契约校验响应", async () => {
    stubUiAssetsFetch(null);

    await expect(deleteGatewayUiAsset(49_923, "asset_1")).rejects.toThrow(
      "Gateway UI 资源列表响应 items 必须是数组，实际为 null",
    );
  });
});

describe("heartbeat 遇取消立即放弃重试", () => {
  test("AbortError 不触发重试，且不再发第二个请求", async () => {
    const port = 49_924;
    let heartbeatCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), `http://127.0.0.1:${port}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "heartbeat-abort-token" } });
        }
        if (path === "/api/gateway/users/current/heartbeat") {
          heartbeatCalls += 1;
          throw new DOMException("页面已卸载", "AbortError");
        }
        throw new Error(`Unexpected request: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    await expect(heartbeatGatewayUserWithRetry(port)).rejects.toMatchObject({
      name: "AbortError",
    });

    expect(heartbeatCalls).toBe(1);
  });
});

describe("Gateway UI 设置读取边界", () => {
  function stubUiSettingsFetch(expandedPaths: unknown): void {
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), "http://127.0.0.1").pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "ui-settings-token" } });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            data: { kind: "guest", user_id: null, lease_generation: 1 },
            request_id: "req_ui_settings_current",
          });
        }
        return Response.json({
          data: {
            layout: {},
            session_sidebar: {},
            workspace_file_tree: { expanded_paths_by_workspace: expandedPaths },
            gateway_console: {},
            theme: { theme_id: "warm", background: null, resolved_theme: null },
            recent_local_workspace_paths: [],
          },
          request_id: "req_ui_settings",
        });
      },
      { preconnect: originalFetch.preconnect },
    );
  }

  test("展开态载荷损坏时读取接口响亮失败，不产出虚假默认值", async () => {
    stubUiSettingsFetch({ "ws-a": [123] });

    await expect(getGatewayUiSettings(49_920)).rejects.toThrow("含非字符串元素");
  });

  test("合法的展开态读取后按工作区归一去重排序", async () => {
    stubUiSettingsFetch({ "ws-a": ["src", "", "src"] });

    const settings = await getGatewayUiSettings(49_921);
    expect(settings.workspace_file_tree.expanded_paths_by_workspace)
      .toEqual({ "ws-a": ["", "src"] });
  });
});

describe("用户视图状态的信封校验", () => {
  /**
   * 安装只应答凭据与用户视图状态的 fetch 桩；request_id 由 requestId 入参决定，
   * 用 undefined 表示响应里省略该字段。
   */
  function stubViewStateFetch(requestId: unknown, includeData = true): void {
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), "http://127.0.0.1").pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "view-state-token" } });
        }
        const body: Record<string, unknown> = {
          request_id: requestId,
        };
        if (includeData) body.data = null;
        return Response.json(body);
      },
      { preconnect: originalFetch.preconnect },
    );
  }

  test("request_id 为非空字符串时允许权威 data 为 null", async () => {
    stubViewStateFetch("req_view_state");

    expect(await getGatewayUserViewState(49_930, "ws-1", "ses-1")).toBeNull();
    expect(await getLatestGatewayUserViewState(49_930)).toBeNull();
  });

  test("request_id 缺失或非字符串时响亮失败，不再被弱校验放过", async () => {
    for (const requestId of [undefined, 123, true, {}]) {
      stubViewStateFetch(requestId);
      await expect(getGatewayUserViewState(49_931, "ws-1", "ses-1")).rejects.toThrow(
        "后端响应缺少 request_id",
      );
    }
  });

  test("data 字段整个缺失时响亮失败，不把缺字段伪装成 null", async () => {
    stubViewStateFetch("req_view_state_missing_data", false);

    await expect(getGatewayUserViewState(49_932, "ws-1", "ses-1")).rejects.toThrow(
      "后端响应缺少 data 字段",
    );
  });
});

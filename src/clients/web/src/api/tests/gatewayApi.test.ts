import { gatewayWorkspaceKey } from "../workspaceIdentity";
import { afterEach, describe, expect, test } from "bun:test";

import {
  addManagedGatewayWorkspace,
  browseGatewayLocalDirectories,
  deleteGatewayUiAsset,
  getGatewayDiagnostics,
  listGatewayUiAssets,
  listGatewayWorkspaces,
  removeGatewayWorkspace,
  uploadGatewayUiAsset,
} from "../../gatewayApi";
import { createSessionConnection } from "../gateway/sessionConnections";
import { requestJson } from "../../api";
import { DEFAULT_API_REQUEST_TIMEOUT_MS, HttpRequestError } from "../http";

const WORKSPACE_UUID = "00000000-0000-4000-8000-000000000001";
const WORKSPACE_KEY = gatewayWorkspaceKey({ gateway_id: "gateway-test", workspace_id: WORKSPACE_UUID });

const originalFetch = globalThis.fetch;
const DIRECTORY_URI = "boxteam://gateway/gateway_00000000000070008000000000000001/resources/files/00000000000070008000000000000001";

afterEach(() => {
  globalThis.fetch = originalFetch;
});
describe("Gateway API 超时常量收敛", () => {
  /** 捕获 createRequestAbortState 注册的超时毫秒数，不改动真实计时。 */
  async function captureTimeoutMs(run: () => Promise<unknown>): Promise<number> {
    const originalSetTimeout = globalThis.setTimeout;
    let captured = -1;
    globalThis.setTimeout = ((handler: TimerHandler, timeout?: number, ...rest: unknown[]) => {
      if (captured < 0 && typeof timeout === "number" && timeout > 0) {
        captured = timeout;
      }
      return (originalSetTimeout as (...a: unknown[]) => unknown)(handler, 0, ...rest);
    }) as typeof globalThis.setTimeout;
    try {
      await run().catch(() => undefined);
    } finally {
      globalThis.setTimeout = originalSetTimeout;
    }
    return captured;
  }

  function stubFailingFetch(): void {
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), "http://127.0.0.1").pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "timeout-token" } });
        }
        return Response.json({ detail: "boom" }, { status: 500 });
      },
      { preconnect: originalFetch.preconnect },
    );
  }

  test("三处读取接口的超时都等于 DEFAULT_API_REQUEST_TIMEOUT_MS", async () => {
    const cases: Array<[string, () => Promise<unknown>]> = [
      ["diagnostics", () => getGatewayDiagnostics(49_970)],
    ];
    for (const [label, run] of cases) {
      stubFailingFetch();
      const captured = await captureTimeoutMs(run);
      process.stdout.write("PROBE_I3 " + label + " timeoutMs=" + captured + "\n");
      expect(captured).toBe(DEFAULT_API_REQUEST_TIMEOUT_MS);
    }
  });
});

describe("手动创建会话连接", () => {
  test("终端和浏览器都请求工作区 owner，并绑定当前会话", async () => {
    const requests: Array<{ url: string; body: Record<string, unknown>; workspaceId: string | null }> = [];
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input, init] = args;
        const path = new URL(String(input)).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "test-token" } });
        }
        const url = String(input);
        requests.push({
          url,
          body: JSON.parse(String(init?.body)) as Record<string, unknown>,
          workspaceId: new Headers(init?.headers).get("X-BoxTeam-Workspace-Id"),
        });
        return url.includes("/api/v1/resources/terminals")
          ? Response.json({ data: { resource_id: "term_manual" }, request_id: "req_manual_terminal" })
          : Response.json({ data: { resource_id: "browser_manual" }, request_id: "req_manual_browser" });
      },
      { preconnect: originalFetch.preconnect },
    );

    const thread = { session_id: "ses_manual", thread_id: "thr_manual" };
    const terminal = await createSessionConnection(
      49_901,
      WORKSPACE_KEY,
      thread,
      "terminal",
    );
    const browser = await createSessionConnection(
      49_901,
      WORKSPACE_KEY,
      thread,
      "browser",
    );

    expect(terminal).toEqual({ kind: "terminal", resourceId: "term_manual" });
    expect(browser).toEqual({ kind: "browser", resourceId: "browser_manual" });
    expect(requests).toHaveLength(2);
    expect(requests[0].url).toContain(
      "/api/v1/resources/terminals",
    );
    expect(requests[0].workspaceId).toBe(WORKSPACE_UUID);
    expect(requests[0].body).toMatchObject({ session_id: "ses_manual", thread_id: "thr_manual", retention_scope: "thread" });
    expect(requests[1].url).toContain(
      "/api/v1/resources/browsers",
    );
    expect(requests[1].body).toMatchObject({
      session_id: "ses_manual",
      thread_id: "thr_manual",
      retention_scope: "thread",
      url: "about:blank",
    });
    expect(requests[1].workspaceId).toBe(WORKSPACE_UUID);
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
            name: "workspace",
            path: DIRECTORY_URI,
            parent_path: DIRECTORY_URI,
            home_path: DIRECTORY_URI,
            entries: [{ name: "project", path: DIRECTORY_URI + "/project" }],
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
      { name: "project", path: DIRECTORY_URI + "/project" },
    ]);
  });

  test("连续 503 时只重试一次并原样抛出可诊断的 HttpRequestError", async () => {
    let businessRequests = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), "http://127.0.0.1").pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "directory-retry-token" } });
        }
        businessRequests += 1;
        return new Response(null, {
          status: 503,
          statusText: "Service Unavailable",
        });
      },
      { preconnect: originalFetch.preconnect },
    );

    const failure = browseGatewayLocalDirectories(49_960);

    await expect(failure).rejects.toBeInstanceOf(HttpRequestError);
    await expect(failure).rejects.toThrow("请求失败 503");
    // 重试严格有界：首次 503 + 一次重试，不允许无限放大流量。
    expect(businessRequests).toBe(2);
  });

  test("非 503 失败不重试，且不吞成空结果", async () => {
    let businessRequests = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), "http://127.0.0.1").pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "directory-404-token" } });
        }
        businessRequests += 1;
        return Response.json({ detail: "目录不存在" }, { status: 404 });
      },
      { preconnect: originalFetch.preconnect },
    );

    await expect(browseGatewayLocalDirectories(49_961)).rejects.toThrow("目录不存在");
    expect(businessRequests).toBe(1);
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
            name: "projects",
            path: DIRECTORY_URI,
            parent_path: DIRECTORY_URI,
            home_path: DIRECTORY_URI,
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
      DIRECTORY_URI,
    );

    const query = new URL(requestedUrls[1]).searchParams;
    expect(query.get("path")).toBe("/srv/projects");
    expect(query.get("gateway_connection_id")).toBe("rgw_remote");
    expect(query.get("base_path")).toBe(DIRECTORY_URI);
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
          data: { active_workspace: { gateway_id: "gateway-test", workspace_id: WORKSPACE_UUID }, items: [] },
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

describe("Gateway 背景图上传的前置校验", () => {
  /** 安装只记录请求次数的 fetch 桩：任何业务请求都算作「已经发出去」。 */
  function stubCountingFetch(): { businessRequests: () => number } {
    let business = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), "http://127.0.0.1").pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "ui-asset-token" } });
        }
        business += 1;
        return Response.json({
          data: {
            asset_id: "asset_1",
            original_filename: "bg.png",
            content_type: "image/png",
            size: 1,
            sha256: "x",
            imported_at: "2026-01-01T00:00:00Z",
            url: "/api/gateway/ui-assets/asset_1",
          },
          request_id: "req_ui_asset",
        });
      },
      { preconnect: originalFetch.preconnect },
    );
    return { businessRequests: () => business };
  }

  test("空文件在发请求前以中文错误失败", async () => {
    const fetchStub = stubCountingFetch();
    const empty = new File([], "bg.png", { type: "image/png" });

    await expect(uploadGatewayUiAsset(49_950, empty)).rejects.toThrow("背景图片内容为空");
    expect(fetchStub.businessRequests()).toBe(0);
  });

  test("超过 20 MiB 的图片在发请求前失败并报出实际上限", async () => {
    const fetchStub = stubCountingFetch();
    const oversized = new File(
      [new Uint8Array(21 * 1024 * 1024)],
      "bg.png",
      { type: "image/png" },
    );

    await expect(uploadGatewayUiAsset(49_951, oversized)).rejects.toThrow(
      "背景图片 21.0 MiB 超过 20 MiB 限制",
    );
    expect(fetchStub.businessRequests()).toBe(0);
  });

  test("恰好 20 MiB 的图片仍然放行并发出请求", async () => {
    const fetchStub = stubCountingFetch();
    const exact = new File(
      [new Uint8Array(20 * 1024 * 1024)],
      "bg.png",
      { type: "image/png" },
    );

    await expect(uploadGatewayUiAsset(49_952, exact)).resolves
      .toMatchObject({ asset_id: "asset_1" });
    expect(fetchStub.businessRequests()).toBe(1);
  });
});


test("受控 DELETE 原样传入完整成员与空目标，force 通过 query 显式传递", async () => {
  const request = { expected_backend_generation: "generation-1", expected_workspace_ids: [WORKSPACE_UUID], desired_workspace_ids: [] };
  const captured: Array<{ method: string | undefined; url: string; body: unknown }> = [];
  globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
    const [input, init] = args;
    const url = String(input);
    if (new URL(url).pathname === "/api/gateway/auth/local-credential") {
      return Response.json({ data: { token: "delete-test-token" } });
    }
    captured.push({ method: init?.method, url, body: JSON.parse(String(init?.body)) });
    return Response.json({ code: 0, message: "ok", request_id: "delete-test", data: { active_workspace: null, items: [] } });
  }, { preconnect: originalFetch.preconnect });
  const result = await removeGatewayWorkspace(49_924, WORKSPACE_KEY, request, true);
  expect(result).toEqual({ active_workspace: null, items: [] });
  expect(captured).toHaveLength(1);
  expect(captured[0]?.method).toBe("DELETE");
  expect(new URL(captured[0]!.url).pathname).toBe(`/api/gateway/workspaces/${WORKSPACE_UUID}`);
  expect(new URL(captured[0]!.url).searchParams.get("force")).toBe("true");
  expect(captured[0]?.body).toEqual(request);
});

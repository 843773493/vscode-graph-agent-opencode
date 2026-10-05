import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useGatewayExtensionResources } from "./useGatewayExtensionResources";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import type { GatewayResourceItem } from "../../types/backend";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const renderers: ReactTestRenderer[] = [];

function ownerItem(revision: number, scope = "thread"): GatewayResourceItem {
  const thread = { session_id: "ses_deleted_source", thread_id: "thr_exact" };
  return {
    connection_kind: "local", gateway_name: "Gateway", workspace_id: "workspace-owner",
    workspace_name: "工作区", session_id: thread.session_id, session_title: thread.session_id,
    resource: {
      resource_id: "browser_owner", workspace_id: "workspace-owner", kind: "browser",
      source_thread: thread, thread_associations: scope === "thread" ? [thread] : [],
      owner_revision: revision, retention_scope: scope, status: "running",
      reconciliation_status: "verified", created_at: "2026-10-05T00:00:00Z",
      updated_at: "2026-10-05T00:00:00Z", available_actions: ["stop"],
      metadata: revision === 1 ? { title: "旧标题", stale_field: "旧值" } : { title: "新标题" },
    },
  };
}

async function mountOwner() {
  let latest!: ReturnType<typeof useGatewayExtensionResources>;
  function Harness() {
    latest = useGatewayExtensionResources({ apiPort: 49_507, initialResourceKey: null, enabled: true });
    return null;
  }
  await act(async () => { renderers.push(create(<Harness />)); });
  return () => latest;
}

function installOwnerFetch(handler: (path: string, init?: RequestInit) => Promise<Response>) {
  installWindow();
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(input instanceof Request ? input.url : String(input), "http://127.0.0.1:49507").pathname;
    if (path === "/api/gateway/auth/local-credential") return apiResponse({ token: "owner-test-token" });
    if (path === "/api/gateway/users/current") return apiResponse({ kind: "guest", user_id: null });
    return await handler(path, init);
  }, { preconnect: originalFetch.preconnect });
}

function installWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: "49507" },
      setInterval: globalThis.setInterval.bind(globalThis),
      clearInterval: globalThis.clearInterval.bind(globalThis),
    },
  });
}

function apiResponse(data: unknown): Response {
  return new Response(JSON.stringify({
    code: 0,
    message: "ok",
    data,
    request_id: "request-extension-resource-test",
  }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  for (const renderer of renderers.splice(0)) act(() => renderer.unmount());
  globalThis.fetch = originalFetch;
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

describe("useGatewayExtensionResources 请求协调", () => {
  test("写入等待期间启动的旧轮询不能覆盖完整 owner 回执，已删来源 Session 无需查询", async () => {
    let releaseWrite!: (response: Response) => void;
    let releasePoll!: (response: Response) => void;
    let lists = 0;
    installOwnerFetch(async (path, init) => {
      if (path === "/api/gateway/resources") {
        lists += 1;
        return lists === 1 ? apiResponse({ items: [ownerItem(1)], errors: [] })
          : await new Promise<Response>((resolve) => { releasePoll = resolve; });
      }
      expect(path).toBe("/api/v1/resources/browser_owner/promote");
      expect(new Headers(init?.headers).get("X-BoxTeam-Workspace-Id")).toBe("workspace-owner");
      expect(JSON.parse(String(init?.body))).toEqual({ expected_revision: 1 });
      return await new Promise<Response>((resolve) => { releaseWrite = resolve; });
    });
    const latest = await mountOwner();
    const entry = latest().entries[0]!;
    let writing!: Promise<void>;
    let polling!: Promise<void>;
    await act(async () => { writing = latest().ownerControl(entry, "promote"); });
    await act(async () => { polling = latest().refresh(); });
    await act(async () => { releaseWrite(apiResponse(ownerItem(2, "workspace").resource)); await writing; });
    expect(latest().entries[0]!.resource.owner_revision).toBe(2);
    expect(latest().entries[0]!.resource.metadata).toEqual({ title: "新标题" });
    expect(latest().entries[0]!.resource.thread_associations).toEqual([]);
    await act(async () => { releasePoll(apiResponse({ items: [ownerItem(1)], errors: [] })); await polling; });
    expect(latest().entries[0]!.resource.owner_revision).toBe(2);
    expect(latest().loading).toBe(false);
  });

  test("owner 写入失败后重新读取权威清单并保留错误", async () => {
    let lists = 0;
    installOwnerFetch(async (path) => {
      if (path === "/api/gateway/resources") {
        lists += 1;
        return apiResponse({ items: [ownerItem(lists === 1 ? 1 : 3, "workspace")], errors: [] });
      }
      return new Response(JSON.stringify({ detail: "owner revision conflict" }), { status: 409 });
    });
    const latest = await mountOwner();
    let failure: unknown;
    await act(async () => {
      try { await latest().ownerControl(latest().entries[0]!, "stop"); }
      catch (error) { failure = error; }
    });
    expect(failure).toBeInstanceOf(Error);
    expect(lists).toBe(2);
    expect(latest().entries[0]!.resource.owner_revision).toBe(3);
  });

  test("扩展窗口切换初始资源时不重复读取资源列表", async () => {
    installWindow();
    let resourceRequests = 0;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const path = new URL(url, "http://127.0.0.1:49507").pathname;
      if (path === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "local-extension-resource-test-token" });
      }
      if (path === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (path === "/api/gateway/resources") {
        resourceRequests += 1;
        return apiResponse({ items: [], errors: [] });
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    function Harness({ initialResourceKey }: { initialResourceKey: string | null }): React.ReactNode {
      useGatewayExtensionResources({
        apiPort: 49_507,
        initialResourceKey,
        enabled: true,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness initialResourceKey="resource-a" />);
      await new Promise<void>((resolve) => setTimeout(resolve, 20));
    });
    expect(resourceRequests).toBe(1);

    await act(async () => {
      renderer!.update(<Harness initialResourceKey="resource-b" />);
      await new Promise<void>((resolve) => setTimeout(resolve, 20));
    });
    expect(resourceRequests).toBe(1);

    await act(async () => {
      renderer!.unmount();
    });
  });
});

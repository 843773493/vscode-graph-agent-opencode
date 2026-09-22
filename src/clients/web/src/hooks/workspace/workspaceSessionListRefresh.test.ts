import { afterEach, describe, expect, test } from "bun:test";
import { fetchWorkspaceSessionListSnapshot } from "./workspaceSessionListRefresh";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function apiResponse(data: unknown): Response {
  return new Response(JSON.stringify({
    code: 0,
    message: "ok",
    data,
    request_id: "request-test",
  }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

const session = {
  session_id: "ses_catalog_only",
  workspace_id: "ws-test",
  title: "只在目录索引中的会话",
  current_agent_id: "test-agent",
  created_at: "2026-08-31T00:00:00Z",
  updated_at: "2026-08-31T00:00:01Z",
};

describe("工作区会话列表快照", () => {
  test("/sessions 为空时从 session-catalog 根节点回填会话并支持刷新恢复", async () => {
    const requestedUrls: string[] = [];
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      requestedUrls.push(url);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return apiResponse({ token: "local-test-token" });
      }
      if (url.includes("/api/gateway/users/current")) {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (url.includes("/api/v1/sessions?") || url.endsWith("/api/v1/sessions")) {
        return apiResponse({ items: [], next_cursor: null, has_more: false });
      }
      if (url.includes("/api/v1/session-catalog/children")) {
        return apiResponse({
          revision: "catalog-revision",
          parent_node_id: null,
          items: [{
            node_id: session.session_id,
            kind: "session",
            name: session.title,
            session_id: session.session_id,
            parent_node_id: null,
            has_children: false,
          }],
          cursor: null,
          total: 1,
        });
      }
      if (url.includes(`/api/v1/sessions/${session.session_id}`)) {
        return apiResponse(session);
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    const result = await fetchWorkspaceSessionListSnapshot(49_401, "ws-test");

    expect(result.sessions).toEqual([session]);
    expect(requestedUrls.some((url) => url.includes("/session-catalog/children"))).toBe(true);
    expect(requestedUrls.some((url) => url.endsWith(`/sessions/${session.session_id}`))).toBe(true);
  });

  test("并发读取同一工作区时共享同一个会话列表请求", async () => {
    const requestedUrls: string[] = [];
    let releaseListRequest!: () => void;
    const listRequestReleased = new Promise<void>((resolve) => {
      releaseListRequest = resolve;
    });
    let sessionListRequestCount = 0;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      requestedUrls.push(url);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return apiResponse({ token: "local-coalesce-token" });
      }
      if (url.includes("/api/gateway/users/current")) {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (url.includes("/api/v1/sessions")) {
        sessionListRequestCount += 1;
        await listRequestReleased;
        return apiResponse({ items: [session], next_cursor: null, has_more: false });
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    const first = fetchWorkspaceSessionListSnapshot(49_402, "ws-coalesce");
    await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 0));
    const second = fetchWorkspaceSessionListSnapshot(49_402, "ws-coalesce");
    releaseListRequest();
    const [firstResult, secondResult] = await Promise.all([first, second]);

    expect(sessionListRequestCount).toBe(1);
    expect(firstResult.sessions).toEqual([session]);
    expect(secondResult.sessions).toEqual([session]);
    expect(requestedUrls.filter((url) => url.includes("/api/v1/sessions"))).toHaveLength(1);
  });

  test("显式刷新等待已有请求后再读取，多个显式刷新不并发", async () => {
    const releases: Array<() => void> = [];
    let sessionListRequestCount = 0;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return apiResponse({ token: "local-force-token" });
      }
      if (url.includes("/api/gateway/users/current")) {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (url.includes("/api/v1/sessions")) {
        sessionListRequestCount += 1;
        await new Promise<void>((resolve) => releases.push(resolve));
        return apiResponse({ items: [session], next_cursor: null, has_more: false });
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    const first = fetchWorkspaceSessionListSnapshot(49_403, "ws-force");
    await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 0));
    const forcedFirst = fetchWorkspaceSessionListSnapshot(49_403, "ws-force", { force: true });
    const forcedSecond = fetchWorkspaceSessionListSnapshot(49_403, "ws-force", { force: true });
    expect(sessionListRequestCount).toBe(1);

    releases.shift()!();
    await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 0));
    expect(sessionListRequestCount).toBe(2);
    releases.shift()!();
    await Promise.all([first, forcedFirst, forcedSecond]);
  });
});

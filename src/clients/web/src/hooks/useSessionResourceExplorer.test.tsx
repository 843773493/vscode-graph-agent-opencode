import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { WorkspaceNavigationTree } from "../types/backend";
import { useSessionResourceExplorer } from "./useSessionResourceExplorer";
import { useSessionGeneratorResources } from "./sessionResourceExplorer/useSessionGeneratorResources";

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

async function flushEffects(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

describe("useSessionResourceExplorer 自动同步", () => {
  test("目录移动失败时只重读旧父/新父分支并保留树状态", async () => {
    const catalogRequests: string[] = [];
    globalThis.fetch = Object.assign(async (
      input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      const url = input instanceof Request ? input.url : String(input);
      const parsed = new URL(url);
      if (parsed.pathname.includes("/api/gateway/auth/local-credential")) {
        return apiResponse({ token: "local-test-token" });
      }
      if (parsed.pathname.includes("/api/gateway/users/current")) {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (parsed.pathname.includes("/api/gateway/workspace-navigation")) {
        return apiResponse({ revision: "navigation", nodes: [] });
      }
      if (parsed.pathname.includes("/api/gateway/session-generators")) {
        return apiResponse({ revision: "generators", items: [] });
      }
      if (parsed.pathname.includes("/api/v1/session-catalog/children")) {
        catalogRequests.push(parsed.search);
        return apiResponse({
          revision: "catalog",
          parent_node_id: parsed.searchParams.get("parent_node_id"),
          items: [],
          cursor: null,
          total: 0,
        });
      }
      if (
        parsed.pathname === "/api/v1/session-catalog/nodes/ses_move/parent"
        && init?.method === "PATCH"
      ) {
        return new Response(JSON.stringify({ detail: "目录移动被拒绝" }), {
          status: 409,
          headers: { "content-type": "application/json" },
        });
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    let latestExplorer: ReturnType<typeof useSessionResourceExplorer> | null = null;
    function Harness(): React.ReactNode {
      const generatorResources = useSessionGeneratorResources(49_404);
      latestExplorer = useSessionResourceExplorer({
        apiPort: 49_404,
        activeWorkspaceId: "ws-test",
        searchOpen: false,
        searchQuery: "",
        currentSessionId: "",
        workspaceNavigationSyncKey: "ws-test",
        catalogSyncKeys: new Map(),
        catalogRefreshVersions: new Map(),
        generatorResources,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await flushEffects();
      await flushEffects();
    });

    await expect(
      latestExplorer!.moveCatalogNode("ws-test", "ses_move", "fld_new", "fld_old"),
    ).rejects.toThrow("目录移动被拒绝");
    expect(catalogRequests).toEqual(expect.arrayContaining([
      "?limit=100&parent_node_id=fld_old",
      "?limit=100&parent_node_id=fld_new",
    ]));
    act(() => renderer!.unmount());
  });

  test("工作区拓扑键变化会重读导航，迟到旧响应不会覆盖新树", async () => {
    const navigationResolvers: Array<(response: Response) => void> = [];
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return apiResponse({ token: "local-test-token" });
      }
      if (url.includes("/api/gateway/users/current")) {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (url.includes("/api/gateway/workspace-navigation")) {
        return await new Promise<Response>((resolve) => {
          navigationResolvers.push(resolve);
        });
      }
      if (url.includes("/api/gateway/session-generators")) {
        return apiResponse({ revision: "generators", items: [] });
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    let latestNavigation: WorkspaceNavigationTree | null = null;
    function Harness({ syncKey }: { syncKey: string }): React.ReactNode {
      const generatorResources = useSessionGeneratorResources(49_402);
      const explorer = useSessionResourceExplorer({
        apiPort: 49_402,
        activeWorkspaceId: null,
        searchOpen: false,
        searchQuery: "",
        currentSessionId: "",
        workspaceNavigationSyncKey: syncKey,
        catalogSyncKeys: new Map(),
        catalogRefreshVersions: new Map(),
        generatorResources,
      });
      latestNavigation = explorer.navigation;
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness syncKey="ws-1" />);
      await flushEffects();
    });
    expect(navigationResolvers).toHaveLength(1);

    await act(async () => {
      renderer!.update(<Harness syncKey="ws-1\u0000ws-2" />);
      await flushEffects();
    });
    expect(navigationResolvers).toHaveLength(2);

    await act(async () => {
      navigationResolvers[1](apiResponse({ revision: "new", nodes: [] }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect((latestNavigation as WorkspaceNavigationTree | null)?.revision).toBe("new");

    await act(async () => {
      navigationResolvers[0](apiResponse({ revision: "old", nodes: [] }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect((latestNavigation as WorkspaceNavigationTree | null)?.revision).toBe("new");
    act(() => renderer!.unmount());
  });

  test("切换工作区时失效的旧会话定位请求不会污染导航错误", async () => {
    let rejectBreadcrumb: ((reason: Error) => void) | null = null;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return apiResponse({ token: "local-test-token" });
      }
      if (url.includes("/api/gateway/users/current")) {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (url.includes("/api/gateway/workspace-navigation")) {
        return apiResponse({ revision: "navigation", nodes: [] });
      }
      if (url.includes("/api/v1/session-catalog/breadcrumb/")) {
        return await new Promise<Response>((_resolve, reject) => {
          rejectBreadcrumb = reject;
        });
      }
      if (url.includes("/api/gateway/session-generators")) {
        return apiResponse({ revision: "generators", items: [] });
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    let latestNavigationError: string | null = null;
    function Harness({ currentSessionId }: { currentSessionId: string }): React.ReactNode {
      const generatorResources = useSessionGeneratorResources(49_403);
      const explorer = useSessionResourceExplorer({
        apiPort: 49_403,
        activeWorkspaceId: "ws-default",
        searchOpen: false,
        searchQuery: "",
        currentSessionId,
        workspaceNavigationSyncKey: "ws-default",
        catalogSyncKeys: new Map(),
        catalogRefreshVersions: new Map(),
        generatorResources,
      });
      latestNavigationError = explorer.navigationError;
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness currentSessionId="session-from-closed-workspace" />);
      await flushEffects();
      await flushEffects();
    });
    expect(rejectBreadcrumb).not.toBeNull();

    await act(async () => {
      renderer!.update(<Harness currentSessionId="" />);
      await flushEffects();
    });
    await act(async () => {
      rejectBreadcrumb!(new Error("HTTP 404"));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(latestNavigationError).toBeNull();
    act(() => renderer!.unmount());
  });
});

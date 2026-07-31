import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { WorkspaceNavigationTree } from "../types/backend";
import { useSessionResourceExplorer } from "./useSessionResourceExplorer";

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
  test("工作区拓扑键变化会重读导航，迟到旧响应不会覆盖新树", async () => {
    const navigationResolvers: Array<(response: Response) => void> = [];
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return apiResponse({ token: "local-test-token" });
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
      const explorer = useSessionResourceExplorer({
        apiPort: 49_402,
        activeWorkspaceId: null,
        searchOpen: false,
        searchQuery: "",
        currentSessionId: "",
        workspaceNavigationSyncKey: syncKey,
        catalogSyncKeys: new Map(),
        catalogRefreshVersions: new Map(),
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
});

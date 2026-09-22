import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create } from "react-test-renderer";
import type { WorkspaceNavigationTree } from "../../types/backend";
import { useSessionResourceExplorer } from "./useSessionResourceExplorer";
import { useSessionGeneratorResources } from "../sessionResourceExplorer/useSessionGeneratorResources";
import {
  apiResponse,
  explorerProps,
  flushEffects,
  installGatewayFetch,
  mountHarness,
  restoreSessionHookGlobals,
  useSessionResourceExplorerHarness,
  withCatalogDefaults,
  type SessionResourceExplorerHandle,
} from "./sessionHookTestFixtures";

afterEach(restoreSessionHookGlobals);

describe("useSessionResourceExplorer 自动同步", () => {
  test("初始目录同步与激活工作区加载根分支共享同一个请求", async () => {
    let releaseCatalog!: (response: Response) => void;
    let rootCatalogRequests = 0;
    installGatewayFetch(withCatalogDefaults(({ path }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        rootCatalogRequests += 1;
        return new Promise<Response>((resolve) => {
          releaseCatalog = resolve;
        });
      }
      return undefined;
    }));

    const Harness = useSessionResourceExplorerHarness({
      props: explorerProps({
        apiPort: 49_405,
        catalogSyncKeys: new Map([["ws-test", "session-sync"]]),
      }),
    });
    const unmount = await mountHarness(Harness);
    expect(rootCatalogRequests).toBe(1);

    await act(async () => {
      releaseCatalog(apiResponse({
        revision: "catalog",
        parent_node_id: null,
        items: [],
        cursor: null,
        total: 0,
      }));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 10));
    });
    unmount();
  });

  test("当前会话定位复用已加载的根分支，不重复读取目录", async () => {
    const catalogRequests: string[] = [];
    installGatewayFetch(withCatalogDefaults(({ path, url }) => {
      const parentNodeId = new URL(url).searchParams.get("parent_node_id");
      if (path.includes("/api/v1/session-catalog/breadcrumb/")) {
        return apiResponse({
          items: [
            { node_id: "folder-a", kind: "folder", name: "Folder A" },
            { node_id: "session-a", kind: "session", name: "Session A", session_id: "session-a" },
          ],
        });
      }
      if (path.includes("/api/v1/session-catalog/children")) {
        catalogRequests.push(parentNodeId ?? "root");
        return apiResponse({
          revision: "catalog",
          parent_node_id: parentNodeId,
          items: parentNodeId === "folder-a"
            ? [{ node_id: "session-a", kind: "session", name: "Session A", session_id: "session-a" }]
            : [{ node_id: "folder-a", kind: "folder", name: "Folder A", has_children: true }],
          cursor: null,
          total: 1,
        });
      }
      return undefined;
    }));

    const Harness = useSessionResourceExplorerHarness({
      props: explorerProps({ apiPort: 49_407, currentSessionId: "session-a" }),
    });
    const unmount = await mountHarness(Harness, 3);

    expect(catalogRequests.filter((parent) => parent === "root")).toHaveLength(1);
    expect(catalogRequests.filter((parent) => parent === "folder-a")).toHaveLength(1);
    unmount();
  });

  test("当前会话不在已完成缓存分支时会自动重读，避免误报导航故障", async () => {
    let rootCatalogRequests = 0;
    installGatewayFetch(withCatalogDefaults(({ path }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        rootCatalogRequests += 1;
        return apiResponse({
          revision: `catalog-${rootCatalogRequests}`,
          parent_node_id: null,
          items: rootCatalogRequests === 1
            ? []
            : [{
                node_id: "session-new",
                kind: "session",
                name: "新会话",
                session_id: "session-new",
                has_children: false,
              }],
          cursor: null,
          total: rootCatalogRequests === 1 ? 0 : 1,
        });
      }
      return undefined;
    }));

    let explorerHandle: SessionResourceExplorerHandle | null = null;
    const Harness = useSessionResourceExplorerHarness({
      props: explorerProps({ apiPort: 49_408 }),
      onExplorer: (explorer) => {
        explorerHandle = explorer;
      },
    });
    const unmount = await mountHarness(Harness, 3);
    expect(rootCatalogRequests).toBe(1);

    await act(async () => {
      await explorerHandle!.revealSearchResult("ws-test", ["session-new"], "session");
      await flushEffects();
    });

    expect(rootCatalogRequests).toBe(2);
    expect(explorerHandle!.branches.get("ws-test:root")?.items[0]?.session_id)
      .toBe("session-new");
    expect(explorerHandle!.navigationError).toBeNull();
    unmount();
  });

  test("目录移动失败时只重读旧父/新父分支并保留树状态", async () => {
    const catalogRequests: string[] = [];
    installGatewayFetch(withCatalogDefaults(({ path, init, url }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        catalogRequests.push(new URL(url).search);
        return apiResponse({
          revision: "catalog",
          parent_node_id: new URL(url).searchParams.get("parent_node_id"),
          items: [],
          cursor: null,
          total: 0,
        });
      }
      if (
        path === "/api/v1/session-catalog/nodes/ses_move/parent"
        && init?.method === "PATCH"
      ) {
        return new Response(JSON.stringify({ detail: "目录移动被拒绝" }), {
          status: 409,
          headers: { "content-type": "application/json" },
        });
      }
      return undefined;
    }));

    let explorerHandle: SessionResourceExplorerHandle | null = null;
    const Harness = useSessionResourceExplorerHarness({
      props: explorerProps({ apiPort: 49_404 }),
      liveGeneratorResources: true,
      onExplorer: (explorer) => {
        explorerHandle = explorer;
      },
    });
    const unmount = await mountHarness(Harness);

    await expect(
      explorerHandle!.moveCatalogNode("ws-test", "ses_move", "fld_new", "fld_old"),
    ).rejects.toThrow("目录移动被拒绝");
    expect(catalogRequests).toEqual(expect.arrayContaining([
      "?limit=100&parent_node_id=fld_old",
      "?limit=100&parent_node_id=fld_new",
    ]));
    unmount();
  });

  test("工作区拓扑键变化会重读导航，迟到旧响应不会覆盖新树", async () => {
    const navigationResolvers: Array<(response: Response) => void> = [];
    installGatewayFetch(withCatalogDefaults(({ path }) => {
      if (path.includes("/api/gateway/workspace-navigation")) {
        return new Promise<Response>((resolve) => {
          navigationResolvers.push(resolve);
        });
      }
      return undefined;
    }));

    let latestNavigation: WorkspaceNavigationTree | null = null;
    function Harness({ syncKey }: { syncKey: string }): React.ReactNode {
      const generatorResources = useSessionGeneratorResources(49_402);
      const explorer = useSessionResourceExplorer({
        ...explorerProps({
          apiPort: 49_402,
          activeWorkspaceId: null,
          workspaceNavigationSyncKey: syncKey,
        }),
        generatorResources,
      });
      latestNavigation = explorer.navigation;
      return null;
    }

    let renderer: ReturnType<typeof create>;
    await act(async () => {
      renderer = create(<Harness syncKey="ws-1" />);
      await flushEffects();
    });
    expect(navigationResolvers).toHaveLength(1);

    await act(async () => {
      renderer.update(<Harness syncKey="ws-1\u0000ws-2" />);
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
    act(() => renderer.unmount());
  });

  test("切换工作区时失效的旧会话定位请求不会污染导航错误", async () => {
    let rejectBreadcrumb: ((reason: Error) => void) | null = null;
    installGatewayFetch(withCatalogDefaults(({ path }) => {
      if (path.includes("/api/v1/session-catalog/breadcrumb/")) {
        return new Promise<Response>((_resolve, reject) => {
          rejectBreadcrumb = reject;
        });
      }
      return undefined;
    }));

    let latestNavigationError: string | null = null;
    function Harness({ currentSessionId }: { currentSessionId: string }): React.ReactNode {
      const generatorResources = useSessionGeneratorResources(49_403);
      const explorer = useSessionResourceExplorer({
        ...explorerProps({
          apiPort: 49_403,
          activeWorkspaceId: "ws-default",
          workspaceNavigationSyncKey: "ws-default",
          currentSessionId,
        }),
        generatorResources,
      });
      latestNavigationError = explorer.navigationError;
      return null;
    }

    let renderer: ReturnType<typeof create>;
    await act(async () => {
      renderer = create(<Harness currentSessionId="session-from-closed-workspace" />);
      await flushEffects();
      await flushEffects();
    });
    expect(rejectBreadcrumb).not.toBeNull();

    await act(async () => {
      renderer.update(<Harness currentSessionId="" />);
      await flushEffects();
    });
    await act(async () => {
      rejectBreadcrumb!(new Error("HTTP 404"));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(latestNavigationError).toBeNull();
    act(() => renderer.unmount());
  });
});

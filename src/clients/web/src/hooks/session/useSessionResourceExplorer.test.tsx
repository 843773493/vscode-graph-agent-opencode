import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create } from "react-test-renderer";
import type { WorkspaceNavigationTree } from "../../types/backend";
import {
  apiResponse,
  catalogChildrenResponse,
  errorResponse,
  flushEffects,
  installGatewayFetch,
  liveExplorerHarness,
  mountResourceExplorer,
  restoreSessionHookGlobals,
  withCatalogDefaults,
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

    const { unmount } = await mountResourceExplorer({
      props: { apiPort: 49_405, catalogSyncKeys: new Map([["ws-test", "session-sync"]]) },
    });
    expect(rootCatalogRequests).toBe(1);

    await act(async () => {
      releaseCatalog(catalogChildrenResponse("catalog"));
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

    const { unmount } = await mountResourceExplorer({
      props: { apiPort: 49_407, currentSessionId: "session-a" },
      flushes: 3,
    });

    expect(catalogRequests.filter((parent) => parent === "root")).toHaveLength(1);
    expect(catalogRequests.filter((parent) => parent === "folder-a")).toHaveLength(1);
    unmount();
  });

  test("当前会话不在已完成缓存分支时会自动重读，避免误报导航故障", async () => {
    let rootCatalogRequests = 0;
    installGatewayFetch(withCatalogDefaults(({ path }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        rootCatalogRequests += 1;
        return rootCatalogRequests === 1
          ? catalogChildrenResponse("catalog-1")
          : catalogChildrenResponse("catalog-2", [{
              node_id: "session-new",
              kind: "session",
              name: "新会话",
              session_id: "session-new",
              has_children: false,
            }]);
      }
      return undefined;
    }));

    const { explorer, unmount } = await mountResourceExplorer({
      props: { apiPort: 49_408 },
      flushes: 3,
    });
    expect(rootCatalogRequests).toBe(1);

    await act(async () => {
      await explorer().revealSearchResult("ws-test", ["session-new"], "session");
      await flushEffects();
    });

    expect(rootCatalogRequests).toBe(2);
    expect(explorer().branches.get("ws-test:root")?.items[0]?.session_id)
      .toBe("session-new");
    expect(explorer().navigationError).toBeNull();
    unmount();
  });

  test("目录移动失败时只重读旧父/新父分支并保留树状态", async () => {
    const catalogRequests: string[] = [];
    installGatewayFetch(withCatalogDefaults(({ path, init, url }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        catalogRequests.push(new URL(url).search);
        return catalogChildrenResponse(
          "catalog",
          [],
          new URL(url).searchParams.get("parent_node_id"),
        );
      }
      if (
        path === "/api/v1/session-catalog/nodes/ses_move/parent"
        && init?.method === "PATCH"
      ) {
        return errorResponse(409, "目录移动被拒绝");
      }
      return undefined;
    }));

    const { explorer, unmount } = await mountResourceExplorer({
      props: { apiPort: 49_404 },
      liveGeneratorResources: true,
    });

    await expect(
      explorer().moveCatalogNode("ws-test", "ses_move", "fld_new", "fld_old"),
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
    const Harness = liveExplorerHarness({
      apiPort: 49_402,
      activeWorkspaceId: null,
      onExplorer: (value) => {
        latestNavigation = value.navigation;
      },
    });

    let renderer: ReturnType<typeof create>;
    await act(async () => {
      renderer = create(<Harness syncKey="ws-1" />);
      await flushEffects();
    });
    expect(navigationResolvers).toHaveLength(1);

    await act(async () => {
      renderer.update(<Harness syncKey={"ws-1\u0000ws-2"} />);
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
    const Harness = liveExplorerHarness({
      apiPort: 49_403,
      activeWorkspaceId: "ws-default",
      workspaceNavigationSyncKey: "ws-default",
      onExplorer: (value) => {
        latestNavigationError = value.navigationError;
      },
    });

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

  test("定位当前会话失败不写入 navigationError，而是独立 revealError", async () => {
    installGatewayFetch(withCatalogDefaults(({ path }) => {
      if (path.includes("/api/v1/session-catalog/breadcrumb/")) {
        return errorResponse(500, "面包屑读取炸了");
      }
      return undefined;
    }));

    const latest: { navigationError: string | null; revealError: string | null } = {
      navigationError: null,
      revealError: null,
    };
    const Harness = liveExplorerHarness({
      apiPort: 49_409,
      activeWorkspaceId: "ws-default",
      workspaceNavigationSyncKey: "ws-default",
      onExplorer: (value) => {
        latest.navigationError = value.navigationError;
        latest.revealError = value.revealError;
      },
    });

    let renderer: ReturnType<typeof create>;
    await act(async () => {
      renderer = create(
        <Harness currentSessionId="session-hidden" />,
      );
      await flushEffects();
      await flushEffects();
      await flushEffects();
    });

    // 工作区导航本身读到了；定位当前会话失败必须走独立通道，
    // 否则界面会误报「无法加载工作区列表」并把重试指向错误的操作。
    expect(latest.navigationError).toBeNull();
    expect(latest.revealError).toContain("定位当前会话失败");
    expect(latest.revealError).toContain("面包屑读取炸了");
    act(() => renderer.unmount());
  });

  test("目录移动失败且补偿重读也失败时抛出错误必须同时含两条文案", async () => {
    installGatewayFetch(withCatalogDefaults(({ path, init }) => {
      if (
        path === "/api/v1/session-catalog/nodes/ses_move/parent"
        && init?.method === "PATCH"
      ) {
        return errorResponse(409, "目录移动被拒绝");
      }
      if (path.includes("/api/v1/session-catalog/children")) {
        return errorResponse(503, "目录重读崩了");
      }
      return undefined;
    }));

    const { explorer, unmount } = await mountResourceExplorer({
      props: { apiPort: 49_407 },
      flushes: 3,
    });

    let failure: Error | undefined;
    await act(async () => {
      failure = await explorer().moveCatalogNode("ws-test", "ses_move", "fld_new", "fld_old")
        .then(() => undefined, (error: Error) => error);
      await flushEffects();
    });

    // 移动失败与补偿重读失败是两条独立事实，用户必须同时看到，不能只报其中一条。
    expect(failure?.message).toContain("目录移动被拒绝");
    expect(failure?.message).toContain("重新读取会话目录失败");
    expect(failure?.message).toContain("目录重读崩了");
    unmount();
  });

  test("刷新目录顶掉在途分支读取且刷新失败时，分支必须落到失败终态而不是永久加载", async () => {
    let releaseChildren!: (response: Response) => void;
    let childrenCalls = 0;
    installGatewayFetch(withCatalogDefaults(({ path }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        childrenCalls += 1;
        return new Promise<Response>((resolve) => {
          releaseChildren = resolve;
        });
      }
      if (path.includes("/api/v1/session-catalog/refresh")) {
        return errorResponse(500, "目录刷新失败");
      }
      return undefined;
    }));

    const { explorer, unmount } = await mountResourceExplorer({
      props: { apiPort: 49_412 },
      flushes: 1,
    });
    expect(childrenCalls).toBe(1);
    expect(explorer().branches.get("ws-test:root")?.loading).toBe(true);

    await act(async () => {
      await explorer().refreshResourceTree().catch(() => undefined);
      await flushEffects();
    });
    const afterRefreshFailure = explorer().branches.get("ws-test:root");
    expect(afterRefreshFailure?.loading).toBe(false);
    expect(afterRefreshFailure?.error).toContain("目录刷新失败");
    // 被顶掉的在途分支即使随后成功返回也不得覆盖失败终态（请求已失效）。
    await act(async () => {
      releaseChildren(catalogChildrenResponse("catalog"));
      await flushEffects();
      await flushEffects();
    });
    expect(explorer().branches.get("ws-test:root")?.loading).toBe(false);
    unmount();
  });
});

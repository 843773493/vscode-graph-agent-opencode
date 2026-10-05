import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create } from "react-test-renderer";
import type { WorkspaceNavigationTree } from "../../types/backend";
import {
  apiResponse,
  catalogChildrenResponse,
  createTestSessionCatalogOutboxRuntime,
  errorResponse,
  flushEffects,
  installGatewayFetch,
  liveExplorerHarness,
  mountResourceExplorer,
  restoreSessionHookGlobals,
  testGatewayUserAccess,
  TEST_WORKSPACE_REQUEST_TARGET,
  withCatalogDefaults,
} from "./sessionHookTestFixtures";

afterEach(restoreSessionHookGlobals);

function waitForReportedCatalogError(
  runtime: ReturnType<typeof createTestSessionCatalogOutboxRuntime>,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const startedAt = Date.now();
    const check = () => {
      if (runtime.errors.length > 0) {
        resolve();
        return;
      }
      if (Date.now() - startedAt > 3_000) {
        reject(new Error("会话目录 outbox 未上报终态错误"));
        return;
      }
      globalThis.setTimeout(check, 10);
    };
    check();
  });
}

function waitForCatalogPrune(
  runtime: ReturnType<typeof createTestSessionCatalogOutboxRuntime>,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const startedAt = Date.now();
    const check = () => {
      if (runtime.pruneCalls.length > 0) {
        resolve();
        return;
      }
      if (Date.now() - startedAt > 3_000) {
        reject(new Error("已刷新的会话目录分支未触发 outbox prune"));
        return;
      }
      globalThis.setTimeout(check, 10);
    };
    check();
  });
}

describe("useSessionResourceExplorer 自动同步", () => {
  test("恢复的已提交操作在分支刷新失败后保留 outbox 并显示错误", async () => {
    const runtime = createTestSessionCatalogOutboxRuntime();
    await runtime.runtime.submit(
      TEST_WORKSPACE_REQUEST_TARGET,
      testGatewayUserAccess(),
      { kind: "create_folder", name: "恢复后的文件夹" },
      { baseCatalogRevision: 3 },
    );
    installGatewayFetch(withCatalogDefaults(({ path }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        return errorResponse(500, "终态分支刷新故障");
      }
      return undefined;
    }));

    const { explorer, unmount } = await mountResourceExplorer({
      props: {
        apiPort: 49_413,
        sessionCatalogOutboxRuntime: runtime.runtime,
        sessionCatalogOutboxSnapshot: runtime.runtime.getSnapshot(),
        gatewayUserAccess: testGatewayUserAccess(),
      },
    });
    await act(async () => {
      await waitForReportedCatalogError(runtime);
      await flushEffects();
    });

    expect(explorer().branches.get("ws-test:root")?.error).toContain("终态分支刷新故障");
    expect(runtime.errors[0]).toContain("终态分支刷新故障");
    expect(runtime.pruneCalls).toHaveLength(0);
    const restoredOutbox = runtime.runtime.getSnapshot().get("ws-test\u0000guest")?.outbox;
    expect(restoredOutbox?.operations).toHaveLength(1);
    expect(restoredOutbox?.operations[0].state).toBe("committed");
    unmount();
  });

  test("恢复的已提交操作仅在分支 revision 追上回执后 prune", async () => {
    const runtime = createTestSessionCatalogOutboxRuntime();
    await runtime.runtime.submit(
      TEST_WORKSPACE_REQUEST_TARGET,
      testGatewayUserAccess(),
      { kind: "create_folder", name: "等待分支确认的文件夹" },
      { baseCatalogRevision: 3 },
    );
    installGatewayFetch(withCatalogDefaults(({ path, url }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        return apiResponse({
          revision: "catalog-4",
          catalog_revision: 4,
          parent_node_id: new URL(url).searchParams.get("parent_node_id"),
          items: [],
          cursor: null,
          total: 0,
          consistency_warning: null,
        });
      }
      return undefined;
    }));

    const { explorer, unmount } = await mountResourceExplorer({
      props: {
        apiPort: 49_414,
        sessionCatalogOutboxRuntime: runtime.runtime,
        sessionCatalogOutboxSnapshot: runtime.runtime.getSnapshot(),
        gatewayUserAccess: testGatewayUserAccess(),
      },
    });
    await act(async () => {
      await waitForCatalogPrune(runtime);
      await flushEffects();
    });

    expect(explorer().branches.get("ws-test:root")?.catalog_revision).toBe(4);
    expect(runtime.pruneCalls).toEqual([{
      catalogRevision: 4,
      operationIds: ["test_catalog_operation_1"],
    }]);
    expect(runtime.runtime.getSnapshot().get("ws-test\u0000guest")?.outbox.operations)
      .toHaveLength(0);
    unmount();
  });

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
    const runtime = createTestSessionCatalogOutboxRuntime({
      result: () => ({ state: "rejected", errorDetail: "目录移动被拒绝" }),
    });
    installGatewayFetch(withCatalogDefaults(({ path, url }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        const query = new URL(url).searchParams;
        catalogRequests.push(query.toString());
        return catalogChildrenResponse(
          "catalog",
          query.has("parent_node_id")
            ? []
            : [{
                node_id: "ses_move",
                kind: "session",
                name: "待移动会话",
                parent_node_id: null,
                session_id: "ses_move",
                has_children: false,
                node_revision: 7,
                state: "active",
              }],
          query.get("parent_node_id"),
        );
      }
      return undefined;
    }));

    const { explorer, unmount } = await mountResourceExplorer({
      props: {
        apiPort: 49_404,
        sessionCatalogOutboxRuntime: runtime.runtime,
        sessionCatalogOutboxSnapshot: runtime.runtime.getSnapshot(),
        gatewayUserAccess: testGatewayUserAccess(),
      },
    });

    await act(async () => {
      await explorer().moveCatalogNode("ws-test", "ses_move", "fld_new", "fld_old");
      await waitForReportedCatalogError(runtime);
      await flushEffects();
    });
    expect(catalogRequests).toContain("limit=100&parent_node_id=fld_old");
    expect(catalogRequests).toContain("limit=100&parent_node_id=fld_new");
    expect(runtime.errors[0]).toContain("目录移动被拒绝");
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
    const runtime = createTestSessionCatalogOutboxRuntime({
      result: () => ({ state: "rejected", errorDetail: "目录移动被拒绝" }),
    });
    installGatewayFetch(withCatalogDefaults(({ path, url }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        if (!new URL(url).searchParams.has("parent_node_id")) {
          return catalogChildrenResponse("catalog", [{
            node_id: "ses_move",
            kind: "session",
            name: "待移动会话",
            parent_node_id: null,
            session_id: "ses_move",
            has_children: false,
            node_revision: 7,
            state: "active",
          }], null);
        }
        return errorResponse(503, "目录重读崩了");
      }
      return undefined;
    }));

    const { explorer, unmount } = await mountResourceExplorer({
      props: {
        apiPort: 49_407,
        sessionCatalogOutboxRuntime: runtime.runtime,
        sessionCatalogOutboxSnapshot: runtime.runtime.getSnapshot(),
        gatewayUserAccess: testGatewayUserAccess(),
      },
      flushes: 3,
    });

    await act(async () => {
      await explorer().moveCatalogNode("ws-test", "ses_move", "fld_new", "fld_old");
      await waitForReportedCatalogError(runtime);
      await flushEffects();
    });

    // 移动终态拒绝与补偿分支重读失败是两条独立事实，必须同时进入可见错误。
    expect(runtime.errors[0]).toContain("目录移动被拒绝");
    expect(runtime.errors[0]).toContain("重新读取会话目录失败");
    expect(runtime.errors[0]).toContain("目录重读崩了");
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

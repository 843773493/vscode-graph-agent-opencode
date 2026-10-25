import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create } from "react-test-renderer";
import type { SessionCatalogNode, WorkspaceNavigationTree } from "../../types/backend";
import {
  apiSuccessResponse,
  catalogChildrenResponse,
  apiErrorResponse,
  installGatewayFetch,
  restoreGatewayTestEnvironment,
  testGatewayUserAccess,
  TEST_GATEWAY_WORKSPACE_REQUEST_TARGET,
  withDefaultGatewayApiResponses,
} from "../../tests/fixtures/session/gatewayEnvironment";
import {
  createSessionCatalogOutboxTestRuntime,
} from "../../tests/fixtures/session/sessionCatalogOutbox";
import { flushReactEffects } from "../../tests/fixtures/session/asyncControl";
import {
  createLiveSessionResourceExplorerHarness,
  mountSessionResourceExplorerHarness,
} from "../../tests/fixtures/session/sessionResourceExplorer";

afterEach(restoreGatewayTestEnvironment);

const moveNodes: SessionCatalogNode[] = [
  { node_id: "ses_move", kind: "session", name: "待移动会话", parent_node_id: "fld_old",
    session_id: "ses_move", has_children: false, node_revision: 7, state: "active" },
  ...["fld_old", "fld_new"].map((nodeId): SessionCatalogNode => ({
    node_id: nodeId, folder_id: nodeId, kind: "folder", name: nodeId, parent_node_id: null,
    has_children: nodeId === "fld_old", node_revision: 1, state: "active",
  })),
];

function waitForReportedCatalogError(
  runtime: ReturnType<typeof createSessionCatalogOutboxTestRuntime>,
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
  runtime: ReturnType<typeof createSessionCatalogOutboxTestRuntime>,
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
  for (const mode of ["展开", "定位续页"] as const) {
    test(`${mode}只发布一致的移动节点，并保留已定位的父分支分页`, async () => {
      let catalogRevision = 3;
      const nodes = (): SessionCatalogNode[] => {
        const moved: SessionCatalogNode = {
          node_id: "folder-a", folder_id: "folder-a", kind: "folder", name: "移动节点",
          parent_node_id: catalogRevision === 3 ? null : "folder-b",
          has_children: false, node_revision: catalogRevision === 3 ? 1 : 2, state: "active",
        };
        const destination: SessionCatalogNode = {
          node_id: "folder-b", folder_id: "folder-b", kind: "folder", name: "目标目录",
          parent_node_id: null, has_children: mode === "定位续页" || catalogRevision > 3,
          node_revision: 1, state: "active",
        };
        const sibling: SessionCatalogNode = {
          node_id: "folder-c", folder_id: "folder-c", kind: "folder", name: "第一页节点",
          parent_node_id: "folder-b", has_children: false, node_revision: 1, state: "active",
        };
        return [destination, ...(mode === "定位续页" ? [sibling] : []), moved];
      };
      installGatewayFetch(withDefaultGatewayApiResponses(({ path, url }) => {
        if (path === "/api/v1/session-catalog/export") {
          return apiSuccessResponse({ revision: `catalog-${catalogRevision}`, catalog_revision: catalogRevision, items: nodes() });
        }
        if (path === "/api/v1/session-catalog/children") {
          const query = new URL(url).searchParams;
          const parent = query.get("parent_node_id");
          const children = nodes().filter((node) => node.parent_node_id === parent);
          if (mode === "定位续页" && parent === "folder-b" && catalogRevision === 4) {
            return apiSuccessResponse({
              revision: "catalog-4", catalog_revision: 4, parent_node_id: parent,
              items: query.get("cursor") ? children.slice(1) : children.slice(0, 1),
              cursor: query.get("cursor") ? null : "second-page", total: children.length,
            });
          }
          return catalogChildrenResponse(`catalog-${catalogRevision}`, children, parent, catalogRevision);
        }
        return undefined;
      }));
      const { explorer, unmount } = await mountSessionResourceExplorerHarness({});
      await act(async () => {
        await explorer().loadBranch("ws-test", "folder-b");
        catalogRevision = 4;
        if (mode === "展开") await explorer().loadBranch("ws-test", "folder-b");
        else await explorer().revealSearchResult("ws-test", ["folder-b", "folder-a"], "folder");
        await flushReactEffects();
      });
      expect(explorer().branches.get("ws-test:root")?.items.map((node) => node.node_id)).toEqual(["folder-b"]);
      expect(explorer().branches.get("ws-test:folder-b")?.items.map((node) => node.node_id))
        .toEqual(mode === "展开" ? ["folder-a"] : ["folder-c", "folder-a"]);
      const moved = [...explorer().branches.values()].flatMap((branch) => branch.items)
        .filter((node) => node.node_id === "folder-a");
      expect(moved).toHaveLength(1);
      expect(moved[0].node_revision).toBe(2);
      expect(moved[0].parent_node_id).toBe("folder-b");
      unmount();
    });
  }

  test("恢复的已提交操作在分支刷新失败后保留 outbox 并显示错误", async () => {
    const runtime = createSessionCatalogOutboxTestRuntime();
    await runtime.runtime.submit(
      TEST_GATEWAY_WORKSPACE_REQUEST_TARGET,
      testGatewayUserAccess(),
      { kind: "create_folder", name: "恢复后的文件夹" },
      { baseCatalogRevision: 3 },
    );
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        return apiErrorResponse(500, "终态分支刷新故障");
      }
      return undefined;
    }));

    const { explorer, unmount } = await mountSessionResourceExplorerHarness({
      props: {
        apiPort: 49_413,
        sessionCatalogOutboxRuntime: runtime.runtime,
        sessionCatalogOutboxSnapshot: runtime.runtime.getSnapshot(),
        gatewayUserAccess: testGatewayUserAccess(),
      },
    });
    await act(async () => {
      await waitForReportedCatalogError(runtime);
      await flushReactEffects();
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
    const runtime = createSessionCatalogOutboxTestRuntime();
    await runtime.runtime.submit(
      TEST_GATEWAY_WORKSPACE_REQUEST_TARGET,
      testGatewayUserAccess(),
      { kind: "create_folder", name: "等待分支确认的文件夹" },
      { baseCatalogRevision: 3 },
    );
    installGatewayFetch(withDefaultGatewayApiResponses(({ path, url }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        return apiSuccessResponse({
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

    const { explorer, unmount } = await mountSessionResourceExplorerHarness({
      props: {
        apiPort: 49_414,
        sessionCatalogOutboxRuntime: runtime.runtime,
        sessionCatalogOutboxSnapshot: runtime.runtime.getSnapshot(),
        gatewayUserAccess: testGatewayUserAccess(),
      },
    });
    await act(async () => {
      await waitForCatalogPrune(runtime);
      await flushReactEffects();
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
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        rootCatalogRequests += 1;
        return new Promise<Response>((resolve) => {
          releaseCatalog = resolve;
        });
      }
      return undefined;
    }));

    const { unmount } = await mountSessionResourceExplorerHarness({
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
    installGatewayFetch(withDefaultGatewayApiResponses(({ path, url }) => {
      if (path === "/api/v1/session-catalog/export") return apiSuccessResponse({
        revision: "catalog", catalog_revision: 3,
        items: [
          { node_id: "folder-a", kind: "folder", name: "Folder A", has_children: true },
          { node_id: "session-a", kind: "session", name: "Session A", session_id: "session-a", parent_node_id: "folder-a" },
        ],
      });
      const parentNodeId = new URL(url).searchParams.get("parent_node_id");
      if (path.includes("/api/v1/session-catalog/breadcrumb/")) {
        return apiSuccessResponse({
          items: [
            { node_id: "folder-a", kind: "folder", name: "Folder A" },
            { node_id: "session-a", kind: "session", name: "Session A", session_id: "session-a" },
          ],
        });
      }
      if (path.includes("/api/v1/session-catalog/children")) {
        catalogRequests.push(parentNodeId ?? "root");
        return apiSuccessResponse({
          revision: "catalog",
          catalog_revision: 3,
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

    const { unmount } = await mountSessionResourceExplorerHarness({
      props: { apiPort: 49_407, currentSessionId: "session-a" },
      flushes: 3,
    });

    expect(catalogRequests.filter((parent) => parent === "root")).toHaveLength(1);
    expect(catalogRequests.filter((parent) => parent === "folder-a")).toHaveLength(1);
    unmount();
  });

  test("当前会话不在已完成缓存分支时会自动重读，避免误报导航故障", async () => {
    let rootCatalogRequests = 0;
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
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

    const { explorer, unmount } = await mountSessionResourceExplorerHarness({
      props: { apiPort: 49_408 },
      flushes: 3,
    });
    expect(rootCatalogRequests).toBe(1);

    await act(async () => {
      await explorer().revealSearchResult("ws-test", ["session-new"], "session");
      await flushReactEffects();
    });

    expect(rootCatalogRequests).toBe(2);
    expect(explorer().branches.get("ws-test:root")?.items[0]?.session_id)
      .toBe("session-new");
    expect(explorer().navigationError).toBeNull();
    unmount();
  });

  test("目录移动失败时对账旧父/新父分支并保留树状态", async () => {
    const catalogRequests: string[] = [];
    const runtime = createSessionCatalogOutboxTestRuntime({
      result: () => ({ state: "rejected", errorDetail: "目录移动被拒绝" }),
    });
    installGatewayFetch(withDefaultGatewayApiResponses(({ path, url }) => {
      if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
        workspace_id: TEST_GATEWAY_WORKSPACE_REQUEST_TARGET.backend_workspace_id,
        catalog_revision: 4, event_seq_watermark: 0, generation: 1,
      });
      if (path === "/api/v1/session-catalog/export") return apiSuccessResponse({
        revision: "catalog", catalog_revision: 4, items: moveNodes,
      });
      if (path.includes("/api/v1/session-catalog/children")) {
        const query = new URL(url).searchParams;
        catalogRequests.push(query.toString());
        return catalogChildrenResponse(
          "catalog",
          moveNodes.filter((node) => node.parent_node_id === query.get("parent_node_id")),
          query.get("parent_node_id"),
          4,
        );
      }
      return undefined;
    }));

    const { explorer, unmount } = await mountSessionResourceExplorerHarness({
      props: {
        apiPort: 49_404,
        sessionCatalogOutboxRuntime: runtime.runtime,
        sessionCatalogOutboxSnapshot: runtime.runtime.getSnapshot(),
        gatewayUserAccess: testGatewayUserAccess(),
      },
    });

    await act(async () => {
      await explorer().loadBranch("ws-test", "fld_old");
      catalogRequests.length = 0;
      await explorer().moveCatalogNode("ws-test", "ses_move", "fld_new", "fld_old");
      await waitForReportedCatalogError(runtime);
      await flushReactEffects();
    });
    expect(catalogRequests).toContain("limit=100&parent_node_id=fld_old");
    expect(catalogRequests).toContain("limit=100&parent_node_id=fld_new");
    expect(runtime.errors[0]).toContain("目录移动被拒绝");
    unmount();
  });

  test("工作区拓扑键变化会重读导航，迟到旧响应不会覆盖新树", async () => {
    const navigationResolvers: Array<(response: Response) => void> = [];
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (path.includes("/api/gateway/workspace-navigation")) {
        return new Promise<Response>((resolve) => {
          navigationResolvers.push(resolve);
        });
      }
      return undefined;
    }));

    let latestNavigation: WorkspaceNavigationTree | null = null;
    const Harness = createLiveSessionResourceExplorerHarness({
      apiPort: 49_402,
      activeWorkspaceId: null,
      onExplorer: (value) => {
        latestNavigation = value.navigation;
      },
    });

    let renderer: ReturnType<typeof create>;
    await act(async () => {
      renderer = create(<Harness syncKey="ws-1" />);
      await flushReactEffects();
    });
    expect(navigationResolvers).toHaveLength(1);

    await act(async () => {
      renderer.update(<Harness syncKey={"ws-1\u0000ws-2"} />);
      await flushReactEffects();
    });
    expect(navigationResolvers).toHaveLength(2);

    await act(async () => {
      navigationResolvers[1](apiSuccessResponse({ revision: "new", nodes: [] }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect((latestNavigation as WorkspaceNavigationTree | null)?.revision).toBe("new");

    await act(async () => {
      navigationResolvers[0](apiSuccessResponse({ revision: "old", nodes: [] }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect((latestNavigation as WorkspaceNavigationTree | null)?.revision).toBe("new");
    act(() => renderer.unmount());
  });

  test("切换工作区时失效的旧会话定位请求不会污染导航错误", async () => {
    let rejectBreadcrumb: ((reason: Error) => void) | null = null;
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (path.includes("/api/v1/session-catalog/breadcrumb/")) {
        return new Promise<Response>((_resolve, reject) => {
          rejectBreadcrumb = reject;
        });
      }
      return undefined;
    }));

    let latestNavigationError: string | null = null;
    const Harness = createLiveSessionResourceExplorerHarness({
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
      await flushReactEffects();
      await flushReactEffects();
    });
    expect(rejectBreadcrumb).not.toBeNull();

    await act(async () => {
      renderer.update(<Harness currentSessionId="" />);
      await flushReactEffects();
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
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (path.includes("/api/v1/session-catalog/breadcrumb/")) {
        return apiErrorResponse(500, "面包屑读取炸了");
      }
      return undefined;
    }));

    const latest: { navigationError: string | null; revealError: string | null } = {
      navigationError: null,
      revealError: null,
    };
    const Harness = createLiveSessionResourceExplorerHarness({
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
      await flushReactEffects();
      await flushReactEffects();
      await flushReactEffects();
    });

    // 工作区导航本身读到了；定位当前会话失败必须走独立通道，
    // 否则界面会误报「无法加载工作区列表」并把重试指向错误的操作。
    expect(latest.navigationError).toBeNull();
    expect(latest.revealError).toContain("定位当前会话失败");
    expect(latest.revealError).toContain("面包屑读取炸了");
    act(() => renderer.unmount());
  });

  test("目录移动失败且补偿重读也失败时抛出错误必须同时含两条文案", async () => {
    const runtime = createSessionCatalogOutboxTestRuntime({
      result: () => ({ state: "rejected", errorDetail: "目录移动被拒绝" }),
    });
    let failRead = false;
    installGatewayFetch(withDefaultGatewayApiResponses(({ path, url }) => {
      if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
        workspace_id: TEST_GATEWAY_WORKSPACE_REQUEST_TARGET.backend_workspace_id,
        catalog_revision: 4, event_seq_watermark: 0, generation: 1,
      });
      if (path === "/api/v1/session-catalog/export") return apiSuccessResponse({
        revision: "catalog", catalog_revision: 4, items: moveNodes,
      });
      if (path.includes("/api/v1/session-catalog/children")) {
        const parent = new URL(url).searchParams.get("parent_node_id");
        if (failRead && parent !== null) return apiErrorResponse(503, "目录重读崩了");
        return catalogChildrenResponse("catalog", moveNodes.filter((node) => node.parent_node_id === parent), parent, 4);
      }
      return undefined;
    }));

    const { explorer, unmount } = await mountSessionResourceExplorerHarness({
      props: {
        apiPort: 49_407,
        sessionCatalogOutboxRuntime: runtime.runtime,
        sessionCatalogOutboxSnapshot: runtime.runtime.getSnapshot(),
        gatewayUserAccess: testGatewayUserAccess(),
      },
      flushes: 3,
    });

    await act(async () => {
      await explorer().loadBranch("ws-test", "fld_old");
      failRead = true;
      await explorer().moveCatalogNode("ws-test", "ses_move", "fld_new", "fld_old");
      await waitForReportedCatalogError(runtime);
      await flushReactEffects();
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
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        childrenCalls += 1;
        return new Promise<Response>((resolve) => {
          releaseChildren = resolve;
        });
      }
      if (path.includes("/api/v1/session-catalog/refresh")) {
        return apiErrorResponse(500, "目录刷新失败");
      }
      return undefined;
    }));

    const { explorer, unmount } = await mountSessionResourceExplorerHarness({
      props: { apiPort: 49_412 },
      flushes: 1,
    });
    expect(childrenCalls).toBe(1);
    expect(explorer().branches.get("ws-test:root")?.loading).toBe(true);

    await act(async () => {
      await explorer().refreshResourceTree().catch(() => undefined);
      await flushReactEffects();
    });
    const afterRefreshFailure = explorer().branches.get("ws-test:root");
    expect(afterRefreshFailure?.loading).toBe(false);
    expect(afterRefreshFailure?.error).toContain("目录刷新失败");
    // 被顶掉的在途分支即使随后成功返回也不得覆盖失败终态（请求已失效）。
    await act(async () => {
      releaseChildren(catalogChildrenResponse("catalog"));
      await flushReactEffects();
      await flushReactEffects();
    });
    expect(explorer().branches.get("ws-test:root")?.loading).toBe(false);
    unmount();
  });
});

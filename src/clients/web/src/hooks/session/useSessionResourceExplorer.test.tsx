import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create } from "react-test-renderer";
import type { GatewayUserAccess, SessionCatalogNode, WorkspaceNavigationTree } from "../../types/backend";
import { useSessionResourceExplorer } from './useSessionResourceExplorer';
import type { SessionCatalogFailureOwner } from './sessionCatalogOutboxRuntime';
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
  createSessionResourceExplorerProps,
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
  test("刷新工作区回执保留用户折叠态，重新激活才自动展开", async () => {
    installGatewayFetch(withDefaultGatewayApiResponses(() => undefined));
    const props = createSessionResourceExplorerProps();
    let explorer!: ReturnType<typeof useSessionResourceExplorer>;
    function Harness({ workspaces, activeWorkspaceId }: Pick<typeof props, "workspaces" | "activeWorkspaceId">) {
      explorer = useSessionResourceExplorer({ ...props, workspaces, activeWorkspaceId });
      return null;
    }
    let renderer: ReturnType<typeof create> | undefined;
    try {
      await act(async () => { renderer = create(<Harness {...props} />); await flushReactEffects(); });
      expect(explorer.expandedIds.has("workspace:ws-test")).toBe(true);
      await act(async () => { explorer.toggleExpanded("workspace:ws-test"); await flushReactEffects(); });
      expect(explorer.expandedIds.has("workspace:ws-test")).toBe(false);
      // 同一工作区的新回执会重建读取回调，不能将其误认成用户切换工作区。
      await act(async () => {
        renderer!.update(<Harness {...props} workspaces={props.workspaces.map(workspace => ({ ...workspace }))} />);
        await flushReactEffects();
      });
      expect(explorer.expandedIds.has("workspace:ws-test")).toBe(false);
      await act(async () => { renderer!.update(<Harness {...props} activeWorkspaceId={null} />); await flushReactEffects(); });
      await act(async () => { renderer!.update(<Harness {...props} />); await flushReactEffects(); });
      expect(explorer.expandedIds.has("workspace:ws-test")).toBe(true);
    } finally { act(() => renderer?.unmount()); }
  });

  test('同一主体的 access 刷新保留目录 owner，真正切换用户才释放旧 owner', async () => {
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => path === '/api/v1/session-catalog/snapshot'
      ? apiSuccessResponse({ workspace_id: TEST_GATEWAY_WORKSPACE_REQUEST_TARGET.backend_workspace_id, catalog_revision: 4, event_seq_watermark: 0, generation: 1 })
      : undefined));
    const fixture = createSessionCatalogOutboxTestRuntime();
    const registrations: SessionCatalogFailureOwner[] = [];
    const removed: SessionCatalogFailureOwner[] = [];
    const originalRegister = fixture.runtime.registerFailureOwner.bind(fixture.runtime);
    const registration = spyOn(fixture.runtime, 'registerFailureOwner').mockImplementation((access, owner) => {
      registrations.push(owner);
      const unregister = originalRegister(access, owner);
      return () => { removed.push(owner); unregister(); };
    });
    const access = testGatewayUserAccess();
    function Harness({ value }: { value: GatewayUserAccess }) {
      useSessionResourceExplorer(createSessionResourceExplorerProps({
        activeWorkspaceId: null, workspaces: [TEST_GATEWAY_WORKSPACE_REQUEST_TARGET],
        gatewayUserAccess: value, sessionCatalogOutboxRuntime: fixture.runtime,
      }));
      return null;
    }
    let renderer: ReturnType<typeof create> | undefined;
    try {
      await act(async () => { renderer = create(<Harness value={access} />); await flushReactEffects(); });
      expect(registrations).toHaveLength(1);
      const originalOwner = registrations[0];
      await act(async () => {
        renderer!.update(<Harness value={{ ...access, lease_generation: 2, expires_at: '2026-10-30T00:00:00Z' }} />);
        await flushReactEffects();
      });
      expect(registrations).toHaveLength(1);
      expect(removed).toHaveLength(0);
      const handle = await fixture.runtime.openWorkspace(TEST_GATEWAY_WORKSPACE_REQUEST_TARGET, access);
      await act(async () => {
        await originalOwner.refresh(TEST_GATEWAY_WORKSPACE_REQUEST_TARGET, handle.driver.current(), 0, new AbortController().signal);
      });
      await act(async () => { renderer!.update(<Harness value={{ ...access, kind: 'user', user_id: 'another-user' }} />); });
      expect(registrations).toHaveLength(2);
      expect(removed).toEqual([originalOwner]);
      await expect(originalOwner.refresh(TEST_GATEWAY_WORKSPACE_REQUEST_TARGET, handle.driver.current(), 0, new AbortController().signal)).rejects.toThrow('owner 已离开');
    } finally { act(() => renderer?.unmount()); registration.mockRestore(); fixture.runtime.dispose(); }
  });
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

  for (const mode of ["推进", "导出落后", "持续变化", "确认失败", "版本回退", "读取被取代"] as const) {
    test(`跨分支读取期间目录${mode}，只发布同一权威版本`, async () => {
      let revision = 3;
      let racing = false;
      let releaseConfirmation: (() => void) | undefined;
      let confirmationRequested!: () => void;
      const confirming = new Promise<void>(resolve => { confirmationRequested = resolve; });
      const exports: number[] = [];
      const reads: Array<{ parent: string | null; revision: number }> = [];
      const nodes = (): SessionCatalogNode[] => moveNodes.map(node => node.node_id === "ses_move"
        ? { ...node, parent_node_id: revision === 3 ? "fld_old" : "fld_new", node_revision: revision }
        : { ...node, has_children: node.node_id === (revision === 3 ? "fld_old" : "fld_new") });
      installGatewayFetch(withDefaultGatewayApiResponses(({ path, url }) => {
        if (path === "/api/v1/session-catalog/export") {
          if (mode === "确认失败" && revision > 3) return apiErrorResponse(500, "确认目录原始失败");
          const exportedRevision = mode === "导出落后" ? 3 : revision;
          exports.push(exportedRevision);
          const response = apiSuccessResponse({ revision: `catalog-${exportedRevision}`, catalog_revision: exportedRevision, items: nodes() });
          if (mode === "读取被取代" && revision === 4 && !releaseConfirmation) {
            return new Promise<Response>(resolve => {
              releaseConfirmation = () => resolve(response);
              confirmationRequested();
            });
          }
          return response;
        }
        if (path === "/api/v1/session-catalog/children") {
          const parent = new URL(url).searchParams.get("parent_node_id");
          if (racing && parent === "fld_new") {
            revision = mode === "版本回退" ? 2 : revision + 1;
            if (mode === "推进" || mode === "读取被取代") racing = false;
          }
          reads.push({ parent, revision });
          return catalogChildrenResponse(`catalog-${revision}`,
            nodes().filter(node => node.parent_node_id === parent), parent, revision);
        }
        return undefined;
      }));
      const { explorer, unmount } = await mountSessionResourceExplorerHarness({});
      try {
        await act(async () => { await explorer().loadBranch("ws-test", "fld_old"); });
        expect(explorer().branches.get("ws-test:fld_old")?.items.map(node => node.node_id)).toEqual(["ses_move"]);
        racing = true;
        // 新分支读取发生在 export 之后；cached root/old 仍属于前一版。
        await act(async () => {
          if (mode === "读取被取代") {
            const original = explorer().loadBranch("ws-test", "fld_new");
            await confirming;
            // 同一分支的重复展开会去重；另一分支的读取才会真实接管整批发布权。
            await explorer().loadBranch("ws-test", "fld_old");
            releaseConfirmation!();
            expect(await original).toBeUndefined();
          } else if (mode === "推进") {
            // 只推进一次；后续整批重读必须从真实的新版本重新取得各分支。
            await explorer().loadBranch("ws-test", "fld_new");
          } else {
            await expect(explorer().loadBranch("ws-test", "fld_new"))
              .rejects.toThrow(/catalog revision|revision 落后/);
          }
        });
        if (mode === "推进" || mode === "读取被取代") {
          expect(exports).toContain(4);
          expect(reads).toContainEqual({ parent: null, revision: 4 });
          const branches = [...explorer().branches.values()];
          expect(branches.every(branch => branch.catalog_revision === 4 && !branch.loading && branch.error === null)).toBe(true);
          expect(branches.flatMap(branch => branch.items).filter(node => node.node_id === "ses_move")
            .map(node => [node.node_id, node.session_id, node.parent_node_id, node.node_revision, node.state]))
            .toEqual([["ses_move", "ses_move", "fld_new", 4, "active"]]);
        } else {
          expect(explorer().branches.get("ws-test:root")?.error).toMatch(/catalog revision|revision 落后/);
          if (mode === "确认失败") expect(explorer().branches.get("ws-test:root")?.error).toContain("确认目录原始失败");
          if (mode === "版本回退") expect(exports).not.toContain(2);
          expect(explorer().branches.get("ws-test:fld_old")?.items.map(node => node.node_id)).toEqual(["ses_move"]);
          expect(explorer().branches.get("ws-test:fld_new")?.items).toEqual([]);
        }
      } finally { releaseConfirmation?.(); unmount(); }
    });
  }

  for (const mode of ["撤销", "成功页同版", "仍在目录", "其它故障", "导出落后", "导出未推进", "确认失败", "读取被取代"] as const) {
    test(`目录预读后父节点删除：${mode}，只按新权威节点集整批重读`, async () => {
      let revision = 13;
      let racing = false;
      let deleted = false;
      let deletionConfirmed = false;
      let releaseConfirmation: (() => void) | undefined;
      let confirmationRequested!: () => void;
      const confirming = new Promise<void>(resolve => { confirmationRequested = resolve; });
      const exports: number[] = [];
      const reads: Array<{ parent: string | null; revision: number }> = [];
      const nodes = (): SessionCatalogNode[] => deleted
        ? moveNodes.filter(node => node.node_id === "fld_new")
        : moveNodes;
      installGatewayFetch(withDefaultGatewayApiResponses(({ path, url }) => {
        if (path === "/api/v1/session-catalog/export") {
          if (deleted) deletionConfirmed = true;
          if (deleted && mode === "确认失败") return apiErrorResponse(500, "删除确认原始失败");
          const exportedRevision = deleted && mode === "导出落后" ? 12
            : deleted && mode === "导出未推进" ? 13 : revision;
          exports.push(exportedRevision);
          const response = apiSuccessResponse({ revision: `catalog-${exportedRevision}`,
            catalog_revision: exportedRevision, items: mode === "仍在目录" ? moveNodes : nodes() });
          if (deleted && mode === "读取被取代" && !releaseConfirmation) {
            return new Promise<Response>(resolve => {
              releaseConfirmation = () => resolve(response);
              confirmationRequested();
            });
          }
          return response;
        }
        if (path === "/api/v1/session-catalog/children" || path === "/api/v1/session-catalog/refresh") {
          const parent = new URL(url).searchParams.get("parent_node_id");
          // 新分支读取发生在 revision=13 的 export 之后，模拟另一项已提交的递归删除。
          if (racing && parent === null) { deleted = true; revision = 19; racing = false; }
          reads.push({ parent, revision });
          if (deleted && parent === "fld_old") return apiErrorResponse(404, "原父节点已删除");
          if (deleted && mode === "其它故障" && parent === "fld_new") return apiErrorResponse(500, "真实读取故障");
          const pageRevision = deleted && !deletionConfirmed && mode === "成功页同版" ? 13 : revision;
          return catalogChildrenResponse(`catalog-${pageRevision}`,
            nodes().filter(node => node.parent_node_id === parent), parent, pageRevision);
        }
        return undefined;
      }));
      const { explorer, unmount } = await mountSessionResourceExplorerHarness({});
      try {
        await act(async () => {
          await explorer().loadBranch("ws-test", "fld_old");
          await explorer().loadBranch("ws-test", "fld_new");
        });
        expect(explorer().branches.get("ws-test:fld_old")?.items.map(node => node.node_id)).toEqual(["ses_move"]);
        const confirmedRoot = explorer().branches.get("ws-test:root")!.items;
        racing = true;
        await act(async () => {
          if (mode === "读取被取代") {
            const original = explorer().refreshResourceTree();
            await confirming;
            // 确认尚未返回时，旧分支和节点都不能提前删除或发布新版。
            expect(explorer().branches.get("ws-test:fld_old")?.items.map(node => node.node_id)).toEqual(["ses_move"]);
            expect(explorer().branches.get("ws-test:root")!.items).toEqual(confirmedRoot);
            await explorer().loadBranch("ws-test", null);
            releaseConfirmation!();
            await expect(original).rejects.toThrow("已被更新请求取代");
          } else if (mode === "撤销" || mode === "成功页同版") {
            await explorer().refreshResourceTree();
          } else {
            await expect(explorer().refreshResourceTree()).rejects.toThrow("重新读取会话目录失败");
          }
        });
        if (mode === "撤销" || mode === "成功页同版" || mode === "读取被取代") {
          expect(exports).toContain(19);
          expect(reads).toContainEqual({ parent: null, revision: 19 });
          expect(explorer().branches.has("ws-test:fld_old")).toBe(false);
          const branches = [...explorer().branches.values()];
          expect(branches.length).toBeGreaterThan(0);
          expect(branches.every(branch => branch.catalog_revision === 19 && !branch.loading && branch.error === null)).toBe(true);
          expect(branches.flatMap(branch => branch.items).map(node => node.node_id)).toEqual(["fld_new"]);
        } else {
          expect(explorer().branches.get("ws-test:fld_old")?.items.map(node => node.node_id)).toEqual(["ses_move"]);
          expect(explorer().branches.get("ws-test:root")!.items).toEqual(confirmedRoot);
          expect(explorer().branches.get("ws-test:root")?.error).toContain("原父节点已删除");
          if (mode === "其它故障") {
            expect(explorer().branches.get("ws-test:root")?.error).toContain("真实读取故障");
            expect(exports).toEqual([13, 13, 13]);
          }
          if (mode === "确认失败") expect(explorer().branches.get("ws-test:root")?.error).toContain("删除确认原始失败");
        }
      } finally { releaseConfirmation?.(); unmount(); }
    });
  }

  test("恢复的终态分支被新读取取代后保留新 confirmed，只由新读取清理回执", async () => {
    const runtime = createSessionCatalogOutboxTestRuntime();
    await runtime.runtime.submit(TEST_GATEWAY_WORKSPACE_REQUEST_TARGET, testGatewayUserAccess(),
      { kind: "create_folder", name: "恢复的目录" }, { baseCatalogRevision: 3 });
    let started!: () => void;
    const reading = new Promise<void>(resolve => { started = resolve; });
    let release!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    let newest = false;
    installGatewayFetch(withDefaultGatewayApiResponses(async ({ path, url }) => {
      if (path === "/api/v1/session-catalog/children" || path === "/api/v1/session-catalog/refresh") {
        if (!newest) { started(); await held; }
        return catalogChildrenResponse("catalog-4", [], new URL(url).searchParams.get("parent_node_id"), 4);
      }
      return undefined;
    }));
    const mounted = await mountSessionResourceExplorerHarness({ props: {
      sessionCatalogOutboxRuntime: runtime.runtime, sessionCatalogOutboxSnapshot: runtime.runtime.getSnapshot(),
      gatewayUserAccess: testGatewayUserAccess(),
    } });
    try {
      await reading;
      expect(runtime.pruneCalls).toEqual([]);
      await act(async () => {
        newest = true;
        await mounted.explorer().refreshResourceTree();
      });
      expect(runtime.pruneCalls).toEqual([{ catalogRevision: 4, operationIds: ["test_catalog_operation_1"] }]);
      const confirmed = mounted.explorer().branches.get("ws-test:root");
      await act(async () => { release(); await flushReactEffects(); });
      expect(runtime.errors).toEqual([]);
      expect(mounted.explorer().branches.get("ws-test:root")).toEqual(confirmed);
      expect(confirmed?.catalog_revision).toBe(4);
      expect(confirmed?.error).toBeNull();
      expect(runtime.pruneCalls).toHaveLength(1);
      expect(runtime.runtime.getSnapshot().get("ws-test\u0000guest")?.outbox.operations).toEqual([]);
    } finally {
      release();
      mounted.unmount();
    }
  });

  test("本地创建的终态刷新被用户刷新取代时不误报业务失败", async () => {
    const runtime = createSessionCatalogOutboxTestRuntime();
    let hold = false;
    let started!: () => void;
    const reading = new Promise<void>(resolve => { started = resolve; });
    let release!: () => void;
    const delayed = new Promise<void>(resolve => { release = resolve; });
    installGatewayFetch(withDefaultGatewayApiResponses(async ({ path, url }) => {
      if (path === "/api/v1/session-catalog/children") {
        if (hold) { started(); await delayed; }
        return catalogChildrenResponse("catalog-3", [], new URL(url).searchParams.get("parent_node_id"), 3);
      }
      if (path === "/api/v1/session-catalog/refresh") return catalogChildrenResponse("catalog-4", [], null, 4);
      return undefined;
    }));
    const mounted = await mountSessionResourceExplorerHarness({ props: {
      sessionCatalogOutboxRuntime: runtime.runtime, gatewayUserAccess: testGatewayUserAccess(),
    } });
    try {
      await act(async () => {
        hold = true;
        await mounted.explorer().createSessionFolder("ws-test", "本地创建目录");
        await reading;
      });
      expect(runtime.pruneCalls).toEqual([]);
      await act(async () => { await mounted.explorer().refreshResourceTree(); });
      expect(runtime.pruneCalls).toEqual([{ catalogRevision: 4, operationIds: ["test_catalog_operation_1"] }]);
      const confirmed = mounted.explorer().branches.get("ws-test:root");
      await act(async () => { release(); await flushReactEffects(); });
      expect(runtime.errors).toEqual([]);
      expect(mounted.explorer().branches.get("ws-test:root")).toEqual(confirmed);
      expect(confirmed?.catalog_revision).toBe(4);
      expect(confirmed?.error).toBeNull();
      expect(runtime.pruneCalls).toHaveLength(1);
    } finally { release(); mounted.unmount(); }
  });

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

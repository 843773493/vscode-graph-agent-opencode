import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { gatewayWorkspaceKey, type GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { createDeferredResult, flushReactEffects } from "../../tests/fixtures/session/asyncControl";
import {
  apiSuccessResponse,
  catalogChildrenResponse,
  installGatewayFetch,
  restoreGatewayTestEnvironment,
  withDefaultGatewayApiResponses,
} from "../../tests/fixtures/session/gatewayEnvironment";
import { createSessionResourceExplorerProps } from "../../tests/fixtures/session/sessionResourceExplorer";
import { useSessionResourceExplorer } from "./useSessionResourceExplorer";

afterEach(restoreGatewayTestEnvironment);

const ownerA = { gateway_id: "gateway-a", workspace_id: "00000000-0000-0000-0000-000000000001" };
const ownerB = { ...ownerA, gateway_id: "gateway-b" };
const keyA = gatewayWorkspaceKey(ownerA);
const keyB = gatewayWorkspaceKey(ownerB);

function page(name: string, revision = 4): Response {
  return catalogChildrenResponse(`revision-${revision}`, [{
    node_id: name, folder_id: name, kind: "folder", name, parent_node_id: null,
    has_children: true, node_revision: 1, state: "active",
  }], null, revision);
}

async function mountOwners(currentSessionId = "") {
  let current!: ReturnType<typeof useSessionResourceExplorer>;
  function Harness({ workspaces }: { workspaces: readonly GatewayWorkspaceRequestTarget[] }) {
    current = useSessionResourceExplorer(createSessionResourceExplorerProps({
      activeWorkspaceId: null, currentSessionId,
      currentSessionWorkspaceId: currentSessionId ? keyA : null, workspaces,
      workspaceNavigationSyncKey: workspaces.map(gatewayWorkspaceKey).join("\n"),
    }));
    return null;
  }
  let renderer!: ReactTestRenderer;
  const update = async (workspaces: readonly GatewayWorkspaceRequestTarget[]) => {
    await act(async () => {
      renderer.update(<Harness workspaces={workspaces} />);
      await flushReactEffects();
    });
  };
  await act(async () => {
    renderer = create(<Harness workspaces={[ownerA, ownerB]} />);
    await flushReactEffects();
  });
  return { explorer: () => current, update, unmount: () => act(() => renderer.unmount()) };
}

describe("目录页随登记 owner 释放", () => {
  test("未登记的目标仍明确拒绝，不冒充已撤销的旧读取", async () => {
    let reads = 0;
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (path.endsWith("/children")) { reads += 1; return page("unexpected"); }
      return undefined;
    }));
    const mounted = await mountOwners();
    try {
      const unknown = gatewayWorkspaceKey({ ...ownerA, workspace_id: "00000000-0000-0000-0000-000000000099" });
      await expect(mounted.explorer().loadBranch(unknown)).rejects.toThrow("缺少唯一已登记工作区");
      expect(reads).toBe(0);
    } finally { mounted.unmount(); }
  });

  test("移除一个 Gateway 的工作区只释放其目录页与展开态", async () => {
    installGatewayFetch(withDefaultGatewayApiResponses(({ path, init }) => path.endsWith("/children")
      ? page(new Headers(init?.headers).get("X-BoxTeam-Target-Gateway-Id")!) : undefined));
    const mounted = await mountOwners();
    try {
      await act(async () => {
        await mounted.explorer().loadBranch(keyA);
        await mounted.explorer().loadBranch(keyB);
        mounted.explorer().toggleExpanded(`workspace:${keyA}`);
        mounted.explorer().toggleExpanded(`catalog:${keyA}:gateway-a`);
        mounted.explorer().toggleExpanded(`workspace:${keyB}`);
      });
      const retained = mounted.explorer().branches.get(`${keyB}:root`);
      await mounted.update([ownerB]);
      expect(mounted.explorer().branches.has(`${keyA}:root`)).toBe(false);
      expect(mounted.explorer().branches.get(`${keyB}:root`)).toEqual(retained);
      expect(mounted.explorer().expandedIds.has(`workspace:${keyA}`)).toBe(false);
      expect(mounted.explorer().expandedIds.has(`catalog:${keyA}:gateway-a`)).toBe(false);
      expect(mounted.explorer().expandedIds.has(`workspace:${keyB}`)).toBe(true);
    } finally { mounted.unmount(); }
  });

  for (const order of ["旧请求先返回", "新请求先返回"] as const) {
    test(`移除后重登同一 owner：${order}，不复用旧请求或覆盖新页`, async () => {
      const old = createDeferredResult<Response>();
      const fresh = createDeferredResult<Response>();
      let reads = 0;
      let oldSignal: AbortSignal | null | undefined;
      installGatewayFetch(withDefaultGatewayApiResponses(({ path, init }) => {
        if (!path.endsWith("/children")) return undefined;
        reads += 1;
        if (reads === 1) { oldSignal = init?.signal; return old.promise; }
        return fresh.promise;
      }));
      const mounted = await mountOwners();
      let pending: Promise<unknown> | undefined;
      let newer: Promise<unknown> | undefined;
      try {
        await act(async () => { pending = mounted.explorer().loadBranch(keyA); await flushReactEffects(); });
        expect(reads).toBe(1);
        await mounted.update([ownerB]);
        const revoked = oldSignal?.aborted;
        await mounted.update([ownerA, ownerB]);
        await act(async () => { newer = mounted.explorer().loadBranch(keyA); await flushReactEffects(); });
        const readsBeforeOldReply = reads;
        if (order === "旧请求先返回") {
          await act(async () => { old.release(page("old-page")); await pending; await flushReactEffects(); });
          // 旧请求收尾不能删除重新登记后的在途记录，第三次调用继续共用新读取。
          await act(async () => { void mounted.explorer().loadBranch(keyA); await flushReactEffects(); });
          expect(reads).toBe(2);
        }
        await act(async () => { fresh.release(page("new-page", 5)); await flushReactEffects(); });
        if (order === "新请求先返回") {
          await act(async () => { old.release(page("old-page")); await pending; await flushReactEffects(); });
        }
        await act(async () => { await newer; await flushReactEffects(); });
        expect(revoked).toBe(true);
        expect(readsBeforeOldReply).toBe(2);
        expect(mounted.explorer().branches.get(`${keyA}:root`)?.items.map(node => node.node_id)).toEqual(["new-page"]);
        expect(mounted.explorer().branches.get(`${keyA}:root`)?.error).toBeNull();
      } finally {
        old.release(page("old-page")); fresh.release(page("new-page", 5));
        await pending; await newer; mounted.unmount();
      }
    });
  }

  test("预读 export 期间移除 owner，不继续读取其子分支或复活错误页", async () => {
    const exported = createDeferredResult<Response>();
    let exports = 0;
    let children = 0;
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (path.endsWith("/export")) { exports += 1; return exported.promise; }
      if (path.endsWith("/children")) { children += 1; return page("parent"); }
      return undefined;
    }));
    const mounted = await mountOwners();
    let pending: Promise<unknown> | undefined;
    try {
      await act(async () => {
        pending = mounted.explorer().loadBranch(keyA, "parent");
        await flushReactEffects();
      });
      expect(exports).toBe(1);
      await mounted.update([ownerB]);
      await act(async () => {
        exported.release(apiSuccessResponse({ revision: "r4", catalog_revision: 4, items: [{
          node_id: "parent", folder_id: "parent", kind: "folder", name: "parent",
          has_children: true, node_revision: 1, state: "active",
        }] }));
        await pending;
        await flushReactEffects();
      });
      expect(children).toBe(0);
      expect([...mounted.explorer().branches.keys()].filter(key => key.startsWith(`${keyA}:`))).toEqual([]);
    } finally { exported.release(page("unused")); await pending; mounted.unmount(); }
  });

  test("当前会话仍待关闭时移除其工作区，不向已移除的 owner 重新定位", async () => {
    let reads = 0;
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (!path.endsWith("/children")) return undefined;
      reads += 1;
      return catalogChildrenResponse("r4", [{
        node_id: "selected", session_id: "selected", kind: "session", name: "selected",
        parent_node_id: null, has_children: false, node_revision: 1, state: "active",
      }], null, 4);
    }));
    const mounted = await mountOwners("selected");
    try {
      await act(async () => { await mounted.explorer().loadBranch(keyA); await flushReactEffects(); });
      const before = reads;
      await mounted.update([ownerB]);
      expect(mounted.explorer().branches.has(`${keyA}:root`)).toBe(false);
      expect(mounted.explorer().revealError).toBeNull();
      expect(reads).toBe(before);
    } finally { mounted.unmount(); }
  });

  test("卸载中止在途 HTTP 请求，迟到响应不再作为有效目录页交付", async () => {
    const response = createDeferredResult<Response>();
    let signal: AbortSignal | null | undefined;
    installGatewayFetch(withDefaultGatewayApiResponses(({ path, init }) => {
      if (!path.endsWith("/children")) return undefined;
      signal = init?.signal;
      return response.promise;
    }));
    const mounted = await mountOwners();
    let pending: Promise<unknown> | undefined;
    try {
      await act(async () => { pending = mounted.explorer().loadBranch(keyA); await flushReactEffects(); });
      mounted.unmount();
      const aborted = signal?.aborted;
      response.release(page("late"));
      expect(await pending).toBeUndefined();
      expect(aborted).toBe(true);
    } finally { response.release(page("late")); await pending; mounted.unmount(); }
  });

  test("卸载后旧句柄不能重新发起目录读取", async () => {
    let reads = 0;
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (!path.endsWith("/children")) return undefined;
      reads += 1;
      return page("loaded");
    }));
    const mounted = await mountOwners();
    const handle = mounted.explorer();
    mounted.unmount();
    expect(await handle.loadBranch(keyA)).toBeUndefined();
    expect(reads).toBe(0);
  });
});

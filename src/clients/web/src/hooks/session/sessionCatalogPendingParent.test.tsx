import { afterEach, describe, expect, test } from "bun:test";
import React, { useSyncExternalStore } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { SessionCatalogOperationReceipt } from "../../api/session/sessionCatalogOperations";
import type { GatewayWorkspace, SessionCatalogNode } from "../../types/backend";
import type { CatalogOutboxPersistencePort, CatalogOutboxStoredOperation } from "../../state/session/sessionCatalogOutboxStore";
import { createSessionCatalogOutboxRuntime, type SessionCatalogOutboxSnapshot } from "./sessionCatalogOutboxRuntime";
import { applyCatalogOutboxOperationReceipt, catalogOutboxBatchToIntents } from "../../state/session/sessionCatalogOutbox";
import { useSessionCatalogActions } from "../shell/useSessionCatalogActions";
import { useSessionResourceExplorer } from "./useSessionResourceExplorer";
import {
  apiSuccessResponse,
  apiErrorResponse,
  installGatewayFetch,
  restoreGatewayTestEnvironment,
  testGatewayUserAccess,
  TEST_GATEWAY_WORKSPACE_REQUEST_TARGET,
  withDefaultGatewayApiResponses,
} from "../../tests/fixtures/session/gatewayEnvironment";
import {
  createSessionResourceExplorerProps,
} from "../../tests/fixtures/session/sessionResourceExplorer";
import { flushReactEffects } from "../../tests/fixtures/session/asyncControl";

const cleanup: Array<() => void> = [];
afterEach(() => {
  for (const dispose of cleanup.splice(0).reverse()) dispose();
  restoreGatewayTestEnvironment();
});

/** 只替换持久端口与 HTTP 结果；两条真实 hook 共用生产 runtime/driver/投影。 */
async function mountPendingParent(options: {
  initialNodes?: readonly SessionCatalogNode[];
  rootPageLimit?: number;
  deferAcceptance?: boolean;
} = {}) {
  let releaseAcceptance = () => {};
  const acceptance = options.deferAcceptance
    ? new Promise<void>((resolve) => { releaseAcceptance = resolve; })
    : null;
  let statusReceiptsVisible = !options.deferAcceptance;
  cleanup.push(() => releaseAcceptance());
  const records = new Map<number, CatalogOutboxStoredOperation>();
  let sequence = 0;
  const persistence: CatalogOutboxPersistencePort = {
    load: async (key) => [...records.values()].filter((record) => record.partition_key === key),
    insert: async (key, operation) => {
      const clientSequence = ++sequence;
      const record: CatalogOutboxStoredOperation = {
        partition_key: key, client_sequence: clientSequence,
        operation: { ...operation, client_sequence: clientSequence },
      };
      records.set(clientSequence, record);
      return record;
    },
    applyReceipts: async (key, receipts) => {
      const updated: CatalogOutboxStoredOperation[] = [];
      for (const receipt of receipts) {
        const existing = [...records.values()].find((record) => record.partition_key === key
          && record.operation.client_operation_id === receipt.operation_id);
        if (!existing) continue;
        const operation = applyCatalogOutboxOperationReceipt(existing.operation, receipt);
        const record = {
          ...existing, operation: { ...operation, client_sequence: existing.client_sequence },
        };
        records.set(record.client_sequence, record);
        updated.push(record);
      }
      return updated;
    },
    delete: async (_key, ids) => {
      for (const [key, record] of records) {
        if (ids.includes(record.operation.client_operation_id)) records.delete(key);
      }
    },
  };
  const receipts = new Map<string, SessionCatalogOperationReceipt>();
  const nodes = new Map((options.initialNodes ?? []).map((node) => [node.node_id, node]));
  const childQueries: Array<string | null> = [];
  const errors: string[] = [];
  const statuses: string[] = [];
  let revision = 7;
  let readFailure: "snapshot" | "children" | "export" | null = null;
  let delayedParent: string | null = null;
  let releaseDelayedRead: (() => void) | undefined;
  let nextExportMutation: (() => void) | undefined;
  const refreshReads: string[] = [];
  installGatewayFetch(withDefaultGatewayApiResponses(({ path, url }) => {
    if (path === "/api/gateway/health") return apiSuccessResponse({ gateway_id: "pending-parent-gateway" });
    if (path === "/api/v1/workspace") {
      return apiSuccessResponse({ workspace_id: TEST_GATEWAY_WORKSPACE_REQUEST_TARGET.backend_workspace_id });
    }
    if (path === "/api/v1/session-catalog/snapshot") {
      if (readFailure === "snapshot") return apiErrorResponse(500, "权威 snapshot 读取失败");
      return apiSuccessResponse({ workspace_id: TEST_GATEWAY_WORKSPACE_REQUEST_TARGET.backend_workspace_id,
        catalog_revision: revision,
        event_seq_watermark: revision - 7, generation: 1 });
    }
    if (path === "/api/v1/session-catalog/navigation-events") {
      return apiSuccessResponse({ workspace_id: TEST_GATEWAY_WORKSPACE_REQUEST_TARGET.backend_workspace_id,
        event_seq_watermark: revision - 7, items: [], next_cursor: null,
        has_more: false, cursor_gone: revision > 7 });
    }
    if (path === "/api/v1/session-catalog/export") {
      if (readFailure === "export") return apiErrorResponse(500, "权威 export 读取失败");
      refreshReads.push("export");
      const response = apiSuccessResponse({ revision: `catalog-${revision}`, catalog_revision: revision, items: [...nodes.values()] });
      // 模拟完整节点集冻结后、根页请求前的另一个客户端提交。
      if (nextExportMutation) {
        nextExportMutation(); nextExportMutation = undefined; revision += 1;
      }
      return response;
    }
    if (path === "/api/v1/session-catalog/refresh") {
      refreshReads.push("refresh");
      const items = [...nodes.values()].filter((node) => node.parent_node_id === null);
      return apiSuccessResponse({ revision: `catalog-${revision}`, catalog_revision: revision,
        parent_node_id: null, items, cursor: null, total: items.length, consistency_warning: null });
    }
    if (path === "/api/v1/session-catalog/children") {
      if (readFailure === "children") return apiErrorResponse(500, "权威 children 读取失败");
      const parent = new URL(url).searchParams.get("parent_node_id");
      childQueries.push(parent);
      if (parent !== null && !nodes.has(parent)) return apiErrorResponse(404, `会话目录节点不存在: ${parent}`);
      const items = [...nodes.values()].filter((node) => node.parent_node_id === parent);
      const limit = parent === null ? options.rootPageLimit ?? items.length : items.length;
      const response = apiSuccessResponse({
        revision: `catalog-${revision}`, catalog_revision: revision,
        parent_node_id: parent, items: items.slice(0, limit),
        cursor: items.length > limit ? "root-more" : null, total: items.length,
        consistency_warning: null,
      });
      if (parent === delayedParent && parent !== null) {
        delayedParent = null;
        return new Promise<Response>((resolve) => { releaseDelayedRead = () => resolve(response); });
      }
      return response;
    }
    return undefined;
  }));
  const runtime = createSessionCatalogOutboxRuntime({
    apiPort: 49_459, openPersistence: async () => persistence,
    reconcileIntervalMs: 60_000, onError: (message) => errors.push(message),
    adapter: {
      enqueue: async (_port, target, intents) => {
        const accepted = intents.map((intent) => {
          const receipt: SessionCatalogOperationReceipt = {
            operation_id: intent.client_operation_id, client_sequence: intent.client_sequence,
            queue_seq: intent.client_sequence, kind: intent.kind, state: "queued",
            created_node_id: intent.kind === "create_folder"
              ? `fld_${String(intent.client_sequence).padStart(32, "0")}` : null,
            committed_catalog_revision: null,
            error_code: null, error_detail: null, settlement: null,
            receipt_revision: 1, updated_at: "2026-10-08T00:00:00Z",
          };
          receipts.set(receipt.operation_id, receipt);
          return receipt;
        });
        if (acceptance) await acceptance;
        statusReceiptsVisible = true;
        return { workspace_id: target.backend_workspace_id!, accepted_count: accepted.length,
          receipts: accepted, created_node_ids: Object.fromEntries(accepted
            .filter((receipt) => receipt.created_node_id)
            .map((receipt) => [receipt.operation_id, receipt.created_node_id!])) };
      },
      queryStatus: async (_port, target, ids) => ({
        workspace_id: target.backend_workspace_id!, catalog_revision: revision,
        items: statusReceiptsVisible ? ids.flatMap((id) => receipts.get(id) ? [receipts.get(id)!] : []) : [],
        unknown_operation_ids: ids.filter((id) => !statusReceiptsVisible || !receipts.has(id)),
      }),
    },
  });
  const access = testGatewayUserAccess();
  runtime.activatePrincipal(access);
  cleanup.push(() => runtime.dispose());

  const workspaces = [{ ...TEST_GATEWAY_WORKSPACE_REQUEST_TARGET, name: "待提交父目录工作区" }] as GatewayWorkspace[];
  const sessions = new Map();
  let explorer!: ReturnType<typeof useSessionResourceExplorer>;
  let actions!: ReturnType<typeof useSessionCatalogActions>;
  let heldSnapshot: SessionCatalogOutboxSnapshot | null = null;
  function Harness() {
    const liveSnapshot = useSyncExternalStore(runtime.subscribe, runtime.getSnapshot);
    const snapshot = heldSnapshot ?? liveSnapshot;
    explorer = useSessionResourceExplorer({ ...createSessionResourceExplorerProps({
      sessionCatalogOutboxRuntime: runtime, sessionCatalogOutboxSnapshot: snapshot,
      gatewayUserAccess: access,
    }) });
    actions = useSessionCatalogActions({
      apiPort: 49_459, activeGatewayWorkspaceId: "ws-test", activeSessionId: null,
      sessionsByWorkspace: sessions, gatewayWorkspaces: workspaces,
      confirm: async () => true, setStatus: (message) => statuses.push(message),
      sessionCatalogOutboxRuntime: runtime, gatewayUserAccess: access,
      activateGatewayWorkspace: async () => undefined, createSession: async () => { throw new Error("不应创建会话"); },
      openWorkspaceSession: async () => undefined, removeGatewayWorkspace: async () => undefined,
      deleteSession: async () => undefined, renameSession: async () => undefined,
      forkSessionContext: async () => { throw new Error("此导航测试未装配 fork"); }, setSessionParent: async () => undefined,
      readSessionSelectionIntent: () => 0,
    });
    return null;
  }
  let renderer!: ReactTestRenderer;
  await act(async () => { renderer = create(<Harness />); await flushReactEffects(); });
  cleanup.push(() => act(() => renderer.unmount()));
  const handle = await runtime.openWorkspace(TEST_GATEWAY_WORKSPACE_REQUEST_TARGET, access);
  return {
    explorer: () => explorer, actions: () => actions, handle, receipts, nodes, errors, statuses, childQueries, refreshReads,
    releaseAcceptance: () => releaseAcceptance(),
    revealStatusReceipts: () => { statusReceiptsVisible = true; },
    holdSnapshotUntilPrune: () => { heldSnapshot = runtime.getSnapshot(); },
    releaseSnapshot: async () => {
      heldSnapshot = null;
      await act(async () => { renderer.update(<Harness />); await flushReactEffects(); });
    },
    afterNextExport: (mutation: () => void) => { nextExportMutation = mutation; },
    setReadFailure: (failure: typeof readFailure) => { readFailure = failure; },
    delayChildRead: (parent: string) => {
      delayedParent = parent;
      return () => {
        if (!releaseDelayedRead) throw new Error("迟到分支读取尚未开始");
        releaseDelayedRead();
      };
    },
    settle: async (failure = false) => {
      revision += 1;
      for (const operation of handle.driver.current().operations) {
        const receipt = receipts.get(operation.client_operation_id)!;
        const failed = failure && operation.name !== "独立目录";
        const state = failed
          ? operation.created_by_operation_id ? "dependency_failed" : "rejected"
          : "committed";
        const nodeId = `fld_${String(operation.client_sequence).padStart(32, "0")}`;
        receipts.set(receipt.operation_id, { ...receipt, state, receipt_revision: 2,
          created_node_id: nodeId,
          committed_catalog_revision: failed ? null : revision,
          error_code: failed ? "parent_creation_failed" : null,
          error_detail: failed ? "父目录创建被明确拒绝" : null });
        if (!failed) {
          const parent = operation.created_by_operation_id
            ? handle.driver.current().operations.find((item) => item.client_operation_id === operation.created_by_operation_id)
            : null;
          nodes.set(nodeId, { node_id: nodeId, folder_id: nodeId, kind: "folder",
            name: operation.name!, parent_node_id: parent ? `fld_${String(parent.client_sequence).padStart(32, "0")}` : operation.parent_node_id,
            has_children: false, node_revision: 1, state: "active" });
        }
      }
      await act(async () => {
        await handle.driver.reconcile();
        await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 850));
        await flushReactEffects();
      });
    },
  };
}

describe("未提交父 Folder 的两条真实 Web 入口", () => {
  for (const receiptEntry of ["enqueue", "status"] as const) {
    test(`${receiptEntry} 回执换成预留 ID 时迁移展开父子，确认清理后仍可展开`, async () => {
      const mounted = await mountPendingParent({ deferAcceptance: true });
      await act(async () => {
        await mounted.explorer().createSessionFolder("ws-test", "父目录");
      });
      const parent = mounted.handle.driver.current().operations[0]!;
      const localParentKey = `catalog:ws-test:${parent.client_operation_id}`;
      await act(async () => {
        mounted.explorer().toggleExpanded(localParentKey, "ws-test", parent.client_operation_id);
        await mounted.actions().createSessionFolder("ws-test", parent.client_operation_id, "子目录");
      });
      const child = mounted.handle.driver.current().operations[1]!;
      const localChildKey = `catalog:ws-test:${child.client_operation_id}`;
      const otherWorkspaceKey = `catalog:ws-other:${parent.client_operation_id}`;
      await act(async () => {
        mounted.explorer().toggleExpanded(localChildKey, "ws-test", child.client_operation_id);
        mounted.explorer().toggleExpanded(otherWorkspaceKey);
        await mounted.explorer().createSessionFolder("ws-test", "关闭目录");
      });
      const closed = mounted.handle.driver.current().operations[2]!;
      expect(parent.created_node_id).toBeNull();
      expect(child.created_node_id).toBeNull();
      expect(mounted.explorer().branches.get(`ws-test:${parent.client_operation_id}`)?.items[0]?.node_id)
        .toBe(child.client_operation_id);
      // enqueue 路径故意不消费受理/提交的中间镜像，让成功 prune 先删掉最后一份映射。
      if (receiptEntry === "enqueue") mounted.holdSnapshotUntilPrune();
      let statusReconciliation: ReturnType<typeof mounted.handle.driver.reconcile> | undefined;
      await act(async () => {
        if (receiptEntry === "status") {
          mounted.revealStatusReceipts();
          // reconcile 发布状态回执后仍等待被挂起的 enqueue；先检查投影再释放该请求。
          statusReconciliation = mounted.handle.driver.reconcile();
        }
        else mounted.releaseAcceptance();
        await flushReactEffects();
      });
      mounted.releaseAcceptance();
      const reservedParent = mounted.handle.driver.current().operations[0]!;
      const canonicalParentKey = `catalog:ws-test:${reservedParent.created_node_id}`;
      expect(reservedParent.state).toBe("accepted");
      expect(reservedParent.created_node_id).not.toBeNull();
      if (receiptEntry === "status") {
        expect(mounted.explorer().expandedIds.has(canonicalParentKey)).toBe(true);
        expect(mounted.explorer().expandedIds.has(localParentKey)).toBe(false);
      } else {
        expect(mounted.explorer().expandedIds.has(localParentKey)).toBe(true);
      }
      expect(mounted.explorer().expandedIds.has(otherWorkspaceKey)).toBe(true);
      expect(mounted.explorer().expandedIds.has(`catalog:ws-test:${closed.client_operation_id}`)).toBe(false);
      expect(mounted.childQueries).not.toContain(reservedParent.created_node_id);
      await act(async () => {
        mounted.releaseAcceptance();
        await statusReconciliation;
        await flushReactEffects();
      });
      const acceptedChild = mounted.handle.driver.current().operations[1]!;
      const acceptedClosed = mounted.handle.driver.current().operations[2]!;
      const canonicalChildKey = `catalog:ws-test:${acceptedChild.created_node_id}`;
      expect(acceptedChild.state).toBe("accepted");
      if (receiptEntry === "status") {
        expect(mounted.explorer().expandedIds.has(canonicalChildKey)).toBe(true);
        expect(mounted.explorer().expandedIds.has(localChildKey)).toBe(false);
      }
      expect(mounted.explorer().expandedIds.has(`catalog:ws-test:${acceptedClosed.created_node_id}`)).toBe(false);
      if (receiptEntry === "status") {
        expect(mounted.explorer().branches.get(`ws-test:${reservedParent.created_node_id}`)?.items[0]?.node_id)
          .toBe(acceptedChild.created_node_id!);
      }
      await mounted.settle();
      // shell 创建入口不持有目录页；由同一 explorer owner 读取真实 canonical 父分支后清理。
      await act(async () => {
        await mounted.explorer().reconcileCatalogBranches("ws-test", [null, reservedParent.created_node_id],
          [parent.client_operation_id, child.client_operation_id, closed.client_operation_id]);
        await flushReactEffects();
      });
      expect(mounted.handle.driver.current().operations).toEqual([]);
      await mounted.releaseSnapshot();
      expect(mounted.explorer().expandedIds.has(canonicalParentKey)).toBe(true);
      expect(mounted.explorer().expandedIds.has(canonicalChildKey)).toBe(true);
      expect(mounted.explorer().expandedIds.has(localParentKey)).toBe(false);
      expect(mounted.explorer().expandedIds.has(localChildKey)).toBe(false);
      expect(mounted.explorer().branches.get(`ws-test:${reservedParent.created_node_id}`)?.items[0]?.folder_id)
        .toBe(acceptedChild.created_node_id);
      expect(mounted.errors).toEqual([]);
    });
  }

  for (const entry of ["explorer", "shell"] as const) {
    test(`${entry} 保留创建依赖、本地父子投影和提交后的真实父 ID`, async () => {
      const mounted = await mountPendingParent();
      const createFolder = (name: string, parent: string | null) => entry === "explorer"
        ? mounted.explorer().createSessionFolder("ws-test", name, parent)
        : mounted.actions().createSessionFolder("ws-test", parent, name);
      await act(async () => { await createFolder("父目录", null); });
      const parent = mounted.handle.driver.current().operations[0]!;
      const parentId = parent.created_node_id!;
      // Explorer 消费已打开菜单的原创建引用，shell 消费 acceptance 返回的预留 ID。
      const parentReference = entry === "explorer" ? parent.client_operation_id : parentId;
      await act(async () => { await createFolder("子目录", parentReference); });
      const child = mounted.handle.driver.current().operations[1]!;
      expect(child.created_by_operation_id).toBe(parent.client_operation_id);
      expect(child.depends_on).toEqual([parent.client_operation_id]);
      expect(child.parent_node_id).toBeNull();
      expect(mounted.explorer().branches.get("ws-test:root")?.total).toBe(0);
      const projectedParent = mounted.explorer().branches.get("ws-test:root")?.items[0]!;
      expect(projectedParent.node_id).toBe(parentId);
      expect(projectedParent.client_ref).toBe(parent.client_operation_id);
      expect(projectedParent.folder_id).toBeNull();
      const local = mounted.explorer().branches.get(`ws-test:${parentId}`)!;
      expect(local.local_pending).toBe(true);
      expect(local.catalog_revision).toBeUndefined();
      expect(local.revision).toBe("");
      expect(local.total).toBeNull();
      expect(local.items.map((item) => [item.name, item.parent_node_id, item.pending_state]))
        .toEqual([["子目录", parentId, "accepted"]]);
      await act(async () => {
        await mounted.explorer().loadBranch("ws-test", parentId);
        await mounted.explorer().loadBranch("ws-test", parent.client_operation_id);
      });
      expect(mounted.childQueries).not.toContain(parentId);
      expect(mounted.childQueries).not.toContain(parent.client_operation_id);
      if (entry === "shell") expect(mounted.statuses).toEqual([
        "会话文件夹已保存本地待同步变更，等待提交", "会话文件夹已保存本地待同步变更，等待提交",
      ]);
      await mounted.settle();
      expect(mounted.childQueries).toContain(parentId);
      expect(mounted.explorer().branches.get(`ws-test:${parentId}`)?.items[0]?.name).toBe("子目录");
      expect(mounted.explorer().branches.get(`ws-test:${parentId}`)?.local_pending).toBeUndefined();
      expect(mounted.explorer().branches.has(`ws-test:${parent.client_operation_id}`)).toBe(false);
      const confirmedParent = mounted.explorer().branches.get("ws-test:root")?.items[0]!;
      expect(confirmedParent.folder_id).toBe(parentId);
      expect(confirmedParent.client_ref).toBeNull();
      expect(mounted.errors).toEqual([]);
    });
  }

  test("父创建失败时子项终结，权威重读保留独立成功目录", async () => {
    const mounted = await mountPendingParent();
    await act(async () => { await mounted.explorer().createSessionFolder("ws-test", "父目录"); });
    const parent = mounted.handle.driver.current().operations[0]!;
    await act(async () => {
      await mounted.actions().createSessionFolder("ws-test", parent.created_node_id, "子目录");
      await mounted.explorer().createSessionFolder("ws-test", "独立目录");
    });
    await mounted.settle(true);
    expect(mounted.explorer().branches.get("ws-test:root")?.items.map((item) => item.name))
      .toEqual(["独立目录"]);
    expect(mounted.explorer().branches.has(`ws-test:${parent.created_node_id}`)).toBe(false);
    expect(mounted.errors.some((error) => error.includes("父目录创建被明确拒绝"))).toBe(true);
    expect([...mounted.receipts.values()].map((receipt) => receipt.state))
      .toEqual(["rejected", "dependency_failed", "committed"]);
    expect(mounted.childQueries).not.toContain(parent.created_node_id);
  });
});


describe("失败对账的真实 confirmed owner 闭包", () => {
  test("递归删除撤销旧分支与迟到页，跨父移动和分页外仍存节点保留，手工刷新复用同一对账", async () => {
    const folder = (id: string, parent: string | null, hasChildren = false): SessionCatalogNode => ({
      node_id: id, folder_id: id, kind: "folder", name: id, parent_node_id: parent,
      has_children: hasChildren, node_revision: 1, state: "active",
    });
    const mounted = await mountPendingParent({ rootPageLimit: 1, initialNodes: [
      folder("host", null, true), folder("deleted-child", "host"),
      folder("moved", "host", true), folder("kept-child", "moved"),
      folder("first-root", null), folder("outside", null),
    ] });
    await act(async () => {
      for (const id of ["host", "deleted-child", "moved", "outside"]) await mounted.explorer().loadBranch("ws-test", id);
      mounted.explorer().toggleExpanded("catalog:ws-test:moved");
      await mounted.explorer().createSessionFolder("ws-test", "失败父目录", "host");
      const parent = mounted.handle.driver.current().operations[0]!;
      await mounted.actions().createSessionFolder("ws-test", parent.created_node_id, "失败子目录");
      await mounted.explorer().createSessionFolder("ws-test", "独立目录");
    });
    const expanded = [...mounted.explorer().expandedIds];
    const release = mounted.delayChildRead("deleted-child");
    let lateRead!: Promise<unknown>;
    await act(async () => { lateRead = mounted.explorer().loadBranch("ws-test", "deleted-child"); await flushReactEffects(); });
    mounted.nodes.delete("host");
    mounted.nodes.delete("deleted-child");
    mounted.nodes.set("moved", { ...mounted.nodes.get("moved")!, parent_node_id: "outside", node_revision: 2 });
    const queryOffset = mounted.childQueries.length;
    try {
      mounted.setReadFailure("export");
      await expect(mounted.settle(true)).rejects.toThrow("权威 export 读取失败");
      expect(mounted.explorer().branches.has("ws-test:host")).toBe(true);
      expect(mounted.handle.driver.current().operations.map((operation) => operation.state)).toEqual(["accepted", "accepted", "accepted"]);
      mounted.setReadFailure(null);
      await mounted.settle(true);
      expect(mounted.childQueries.slice(queryOffset)).not.toContain("host");
      expect(mounted.childQueries.slice(queryOffset)).not.toContain("deleted-child");
      expect(mounted.explorer().branches.has("ws-test:host")).toBe(false);
      expect(mounted.explorer().branches.has("ws-test:deleted-child")).toBe(false);
      expect(mounted.explorer().branches.get("ws-test:moved")?.items.map((node) => node.node_id)).toEqual(["kept-child"]);
      expect(mounted.explorer().branches.get("ws-test:outside")?.items.map((node) => node.node_id)).toEqual(["moved"]);
      expect(mounted.explorer().branches.get("ws-test:root")?.cursor).toBe("root-more");
      expect(mounted.explorer().branches.get("ws-test:root")?.items.map((node) => node.node_id)).toEqual(["first-root"]);
      release(); await lateRead;
      mounted.afterNextExport(() => mounted.nodes.set("outside", {
        ...mounted.nodes.get("outside")!, name: "另一客户端的最新根名称", node_revision: 2,
      }));
      await act(async () => { await flushReactEffects(); await mounted.explorer().refreshResourceTree(); });
      expect(mounted.refreshReads.slice(-2)).toEqual(["export", "refresh"]);
      expect(mounted.explorer().branches.has("ws-test:deleted-child")).toBe(false);
      expect(mounted.explorer().branches.get("ws-test:root")?.cursor).toBeNull();
      expect(mounted.explorer().branches.get("ws-test:root")?.items.map((node) => node.name)).toEqual(["first-root", "另一客户端的最新根名称", "独立目录"]);
      expect([...mounted.explorer().expandedIds]).toEqual(expanded);
      expect(mounted.handle.driver.current().operations).toEqual([]);
    } finally {
      release(); await lateRead;
    }
  });

  test("失败对账先发布最新confirmed再清理闭包并投影独立pending", async () => {
    const { addCatalogOutboxIntent, assignCatalogOutboxOperationSequence,
      catalogOutboxPartitionKey, catalogOutboxBatchToIntents, createCatalogOutbox, markCatalogOutboxOperationPersisted } =
      await import('../../state/session/sessionCatalogOutbox');
    const ids = ['a', 'b', 'd', 'c'].map((suffix) => 'op_' + suffix.repeat(32));
    const partition = { gatewayId: 'failure-gateway', workspaceId: TEST_GATEWAY_WORKSPACE_REQUEST_TARGET.backend_workspace_id!, principal: 'guest' };
    let seed = createCatalogOutbox(partition);
    const intents = [
      { kind: 'rename_node' as const, targetNodeId: 'cnode_a', name: 'A已提交' },
      { kind: 'create_folder' as const, name: 'B失败目录' },
      { kind: 'move_node' as const, targetNodeId: 'cnode_b', parentCreatedByOperationId: ids[1] },
      { kind: 'rename_node' as const, targetNodeId: 'cnode_c', name: 'C独立pending' },
    ];
    for (const [index, intent] of intents.entries()) {
      seed = addCatalogOutboxIntent(seed, ids[index]!, intent, { baseCatalogRevision: 7,
        ...(intent.kind === 'create_folder' ? {} : { expectedRevision: 3 }),
        ...(index === 2 ? { dependsOn: [ids[1]!] } : {}) });
      seed = markCatalogOutboxOperationPersisted(assignCatalogOutboxOperationSequence(seed, ids[index]!, index + 1), ids[index]!);
    }
    const originalC = seed.operations[3]!;
    const records = new Map(seed.operations.map((operation) => {
      if (operation.client_sequence === null) throw new Error("恢复用例的持久 operation 缺少序号");
      return [operation.client_operation_id,
        { partition_key: catalogOutboxPartitionKey(partition), client_sequence: operation.client_sequence,
          operation: { ...operation, client_sequence: operation.client_sequence } }] as const;
    }));
    const deleted: string[][] = [];
    const persistence: CatalogOutboxPersistencePort = {
      load: async () => [...records.values()],
      insert: async () => { throw new Error('恢复用例不得创建新intent'); },
      applyReceipts: async (_key, receipts) => {
        const updated: CatalogOutboxStoredOperation[] = [];
        for (const receipt of receipts) {
          const existing = records.get(receipt.operation_id);
          if (!existing) continue;
          const operation = applyCatalogOutboxOperationReceipt(existing.operation, receipt);
          const record = {
            ...existing, operation: { ...operation, client_sequence: existing.client_sequence },
          };
          records.set(receipt.operation_id, record);
          updated.push(record);
        }
        return updated;
      },
      delete: async (_key, operationIds) => {
        deleted.push([...operationIds]);
        for (const id of operationIds) records.delete(id);
      },
    };
    const receipt = (index: number, state: SessionCatalogOperationReceipt['state']): SessionCatalogOperationReceipt => ({
      operation_id: ids[index]!, client_sequence: index + 1, queue_seq: index + 1,
      kind: intents[index]!.kind, state, created_node_id: null,
      committed_catalog_revision: state === 'committed' ? 9 : null,
      error_code: state === 'rejected' ? 'revision_changed' : state === 'dependency_failed' ? 'dependency_failed' : null,
      error_detail: state === 'rejected' ? 'B被其它客户端修改' : null,
      settlement: null, receipt_revision: 2, updated_at: '2026-10-09T00:00:00Z',
    });
    let releaseStatus!: () => void;
    const statusGate = new Promise<void>((resolve) => { releaseStatus = resolve; });
    let releaseBranch!: () => void;
    const branchGate = new Promise<void>((resolve) => { releaseBranch = resolve; });
    let branchRequested!: () => void;
    const branchStarted = new Promise<void>((resolve) => { branchRequested = resolve; });
    let dispatched!: () => void;
    const dispatchStarted = new Promise<void>((resolve) => { dispatched = resolve; });
    let refreshing = false;
    const nodes = ['a', 'b', 'c', 'external'].map((suffix): SessionCatalogNode => ({
      node_id: 'cnode_' + suffix, kind: 'folder', name: suffix === 'a' ? 'A已提交' : suffix,
      parent_node_id: null, has_children: false, node_revision: 3, state: 'active',
    }));
    installGatewayFetch(withDefaultGatewayApiResponses(async ({ path }) => {
      if (path === '/api/gateway/health') return apiSuccessResponse({ gateway_id: partition.gatewayId });
      if (path === '/api/v1/workspace') return apiSuccessResponse({ workspace_id: partition.workspaceId });
      if (path === '/api/v1/session-catalog/snapshot') return apiSuccessResponse({ workspace_id: partition.workspaceId,
        catalog_revision: 12, event_seq_watermark: 4, generation: 1 });
      if (path === '/api/v1/session-catalog/children') {
        if (refreshing) { branchRequested(); await branchGate; }
        const items = nodes.map((node) => refreshing && node.node_id === 'cnode_external'
          ? { ...node, name: '外部客户端已提交', node_revision: 4 } : node);
        return apiSuccessResponse({ revision: refreshing ? 'catalog-12' : 'catalog-7',
          catalog_revision: refreshing ? 12 : 7, parent_node_id: null, items,
          total: items.length, cursor: null, consistency_warning: null });
      }
      return undefined;
    }));
    const errors: string[] = [];
    let explorer!: ReturnType<typeof useSessionResourceExplorer>;
    const dispatchedFacts: Array<{ intents: unknown; names: string[]; removed: string[][] }> = [];
    const runtime = createSessionCatalogOutboxRuntime({ apiPort: 49_459,
      openPersistence: async () => persistence, reconcileIntervalMs: 60_000,
      onError: (message) => errors.push(message), adapter: {
        queryStatus: async (_port, target) => {
          await statusGate;
          return { workspace_id: target.backend_workspace_id!, catalog_revision: 12,
            items: [receipt(0, 'committed'), receipt(1, 'rejected'), receipt(2, 'dependency_failed')],
            unknown_operation_ids: [ids[3]!] };
        },
        enqueue: async (_port, target, submitted) => {
          dispatchedFacts.push({ intents: submitted,
            names: explorer.branches.get('ws-test:root')?.items.map((node) => node.name) ?? [],
            removed: deleted.map((group) => [...group]) });
          dispatched();
          return { workspace_id: target.backend_workspace_id!, accepted_count: submitted.length,
            receipts: [receipt(3, 'queued')], created_node_ids: {} };
        },
      } });
    const access = testGatewayUserAccess();
    runtime.activatePrincipal(access);
    let renderer!: ReactTestRenderer;

    function Harness() {
      const snapshot = useSyncExternalStore(runtime.subscribe, runtime.getSnapshot);
      explorer = useSessionResourceExplorer({ ...createSessionResourceExplorerProps({ sessionCatalogOutboxRuntime: runtime,
        sessionCatalogOutboxSnapshot: snapshot, gatewayUserAccess: access }) });
      return null;
    }
    try {
      await act(async () => { renderer = create(<Harness />); await flushReactEffects(); });
      await act(async () => { await explorer.loadBranch('ws-test'); });
      const handle = await runtime.openWorkspace(TEST_GATEWAY_WORKSPACE_REQUEST_TARGET, access);
      await act(async () => { explorer.toggleExpanded("catalog:ws-test:cnode_external"); });
      const expandedBefore = [...explorer.expandedIds];
      expect(expandedBefore).toContain("catalog:ws-test:cnode_external");
      refreshing = true;
      await act(async () => { releaseStatus(); await branchStarted; });
      expect(dispatchedFacts).toEqual([]);
      expect(deleted).toEqual([]);
      expect(handle.driver.current().operations.map((operation) => operation.client_operation_id)).toEqual(ids);
      await act(async () => { releaseBranch(); await flushReactEffects(); });
      await act(async () => { await flushReactEffects(); });
      await dispatchStarted;
      expect(dispatchedFacts).toHaveLength(1);
      expect(dispatchedFacts[0]!.names).toContain('外部客户端已提交');
      expect(dispatchedFacts[0]!.names).toContain('A已提交');
      expect(dispatchedFacts[0]!.names).toContain('C独立pending');
      expect(dispatchedFacts[0]!.removed.flat()).toEqual(expect.arrayContaining([ids[1]!, ids[2]!]));
      expect(explorer.branches.get('ws-test:root')?.catalog_revision).toBe(12);
      expect(explorer.branches.get('ws-test:root')?.items.map((node) => node.name)).not.toContain('B失败目录');
      expect([...explorer.expandedIds]).toEqual(expandedBefore);
      const currentC = handle.driver.current().operations.find((operation) => operation.client_operation_id === ids[3])!;
      expect(catalogOutboxBatchToIntents([currentC])).toEqual(catalogOutboxBatchToIntents([originalC]));
      expect(dispatchedFacts[0]!.intents).toEqual(catalogOutboxBatchToIntents([originalC]));
      expect(currentC.base_catalog_revision).toBe(7);
      expect(currentC.client_operation_id).toBe(originalC.client_operation_id);
      expect(errors.some((message) => message.includes('B被其它客户端修改'))).toBe(true);
    } finally {
      releaseStatus(); releaseBranch(); runtime.dispose();
      if (renderer) await act(async () => { renderer.unmount(); });
    }
  });
});

describe("真实 confirmed owner 刷新失败的恢复", () => {
  for (const stage of ["snapshot", "children"] as const) {
    test(`${stage} 读取失败保留 accepted 及依赖，恢复后只清失败闭包`, async () => {
      const mounted = await mountPendingParent();
      await act(async () => { await mounted.explorer().createSessionFolder("ws-test", "父目录"); });
      const parent = mounted.handle.driver.current().operations[0]!;
      await act(async () => {
        await mounted.actions().createSessionFolder("ws-test", parent.created_node_id, "子目录");
        await mounted.explorer().createSessionFolder("ws-test", "独立目录");
        mounted.explorer().toggleExpanded("catalog:ws-test:kept-expansion");
      });
      const accepted = catalogOutboxBatchToIntents(mounted.handle.driver.current().operations);
      mounted.setReadFailure(stage);
      await expect(mounted.settle(true)).rejects.toThrow(`权威 ${stage} 读取失败`);
      await act(async () => {
        await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 800));
        await flushReactEffects();
      });
      expect(catalogOutboxBatchToIntents(mounted.handle.driver.current().operations)).toEqual(accepted);
      expect(mounted.handle.driver.current().operations.map((operation) => operation.state)).toEqual(["accepted", "accepted", "accepted"]);
      expect(mounted.errors.join("；")).toContain("父目录创建被明确拒绝");
      const confirmedRevision = mounted.explorer().branches.get("ws-test:root")?.catalog_revision;
      if (stage === "children") expect(confirmedRevision).toBe(7);
      else expect(confirmedRevision).toBeGreaterThanOrEqual(7);
      mounted.setReadFailure(null);
      await mounted.settle(true);
      expect(mounted.explorer().branches.get("ws-test:root")?.items.map((node) => node.name)).toEqual(["独立目录"]);
      expect(mounted.explorer().expandedIds.has("catalog:ws-test:kept-expansion")).toBe(true);
      expect(mounted.explorer().branches.has(`ws-test:${parent.created_node_id}`)).toBe(false);
      expect(mounted.handle.driver.current().operations.every((operation) => operation.state === "committed")).toBe(true);
    });
  }
});

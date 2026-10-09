import { afterEach, describe, expect, test } from "bun:test";
import React, { useSyncExternalStore } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { SessionCatalogOperationReceipt } from "../../api/session/sessionCatalogOperations";
import type { GatewayWorkspace, SessionCatalogNode } from "../../types/backend";
import type { CatalogOutboxPersistencePort, CatalogOutboxStoredOperation } from "../../state/session/sessionCatalogOutboxStore";
import { createSessionCatalogOutboxRuntime } from "./sessionCatalogOutboxRuntime";
import { useSessionCatalogActions } from "../shell/useSessionCatalogActions";
import { useSessionResourceExplorer } from "./useSessionResourceExplorer";
import {
  apiResponse, emptyGeneratorResources, explorerProps, flushEffects,
  installGatewayFetch, restoreSessionHookGlobals, testGatewayUserAccess,
  TEST_WORKSPACE_REQUEST_TARGET, withCatalogDefaults,
} from "./sessionHookTestFixtures";

const cleanup: Array<() => void> = [];
afterEach(() => {
  for (const dispose of cleanup.splice(0).reverse()) dispose();
  restoreSessionHookGlobals();
});

/** 只替换持久端口与 HTTP 结果；两条真实 hook 共用生产 runtime/driver/投影。 */
async function mountPendingParent() {
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
    delete: async (_key, ids) => {
      for (const [key, record] of records) {
        if (ids.includes(record.operation.client_operation_id)) records.delete(key);
      }
    },
  };
  const receipts = new Map<string, SessionCatalogOperationReceipt>();
  const nodes = new Map<string, SessionCatalogNode>();
  const childQueries: Array<string | null> = [];
  const errors: string[] = [];
  const statuses: string[] = [];
  let revision = 7;
  installGatewayFetch(withCatalogDefaults(({ path, url }) => {
    if (path === "/api/gateway/health") return apiResponse({ gateway_id: "pending-parent-gateway" });
    if (path === "/api/v1/workspace") {
      return apiResponse({ workspace_id: TEST_WORKSPACE_REQUEST_TARGET.backend_workspace_id });
    }
    if (path === "/api/v1/session-catalog/snapshot") {
      return apiResponse({ revision: `catalog-${revision}`, catalog_revision: revision, nodes: [...nodes.values()] });
    }
    if (path === "/api/v1/session-catalog/children") {
      const parent = new URL(url).searchParams.get("parent_node_id");
      childQueries.push(parent);
      return apiResponse({
        revision: `catalog-${revision}`, catalog_revision: revision,
        parent_node_id: parent, items: [...nodes.values()].filter((node) => node.parent_node_id === parent),
        cursor: null, total: [...nodes.values()].filter((node) => node.parent_node_id === parent).length,
        consistency_warning: null,
      });
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
            created_node_id: null, committed_catalog_revision: null,
            error_code: null, error_detail: null, pending_settlement: false,
            receipt_revision: 1, updated_at: "2026-10-08T00:00:00Z",
          };
          receipts.set(receipt.operation_id, receipt);
          return receipt;
        });
        return { workspace_id: target.backend_workspace_id!, accepted_count: accepted.length,
          receipts: accepted, created_node_ids: {} };
      },
      queryStatus: async (_port, target, ids) => ({
        workspace_id: target.backend_workspace_id!, catalog_revision: revision,
        items: ids.flatMap((id) => receipts.get(id) ? [receipts.get(id)!] : []),
        unknown_operation_ids: ids.filter((id) => !receipts.has(id)),
      }),
    },
  });
  const access = testGatewayUserAccess();
  runtime.activatePrincipal(access);
  cleanup.push(() => runtime.dispose());
  const generatorResources = emptyGeneratorResources();
  const workspaces = [{ ...TEST_WORKSPACE_REQUEST_TARGET, name: "待提交父目录工作区" }] as GatewayWorkspace[];
  const sessions = new Map();
  let explorer!: ReturnType<typeof useSessionResourceExplorer>;
  let actions!: ReturnType<typeof useSessionCatalogActions>;
  function Harness() {
    const snapshot = useSyncExternalStore(runtime.subscribe, runtime.getSnapshot);
    explorer = useSessionResourceExplorer({ ...explorerProps({
      sessionCatalogOutboxRuntime: runtime, sessionCatalogOutboxSnapshot: snapshot,
      gatewayUserAccess: access,
    }), generatorResources });
    actions = useSessionCatalogActions({
      apiPort: 49_459, activeGatewayWorkspaceId: "ws-test", activeSessionId: null,
      sessionsByWorkspace: sessions, gatewayWorkspaces: workspaces,
      confirm: async () => true, setStatus: (message) => statuses.push(message),
      sessionCatalogOutboxRuntime: runtime, gatewayUserAccess: access,
      activateGatewayWorkspace: async () => undefined, createSession: async () => { throw new Error("不应创建会话"); },
      openWorkspaceSession: async () => undefined, removeGatewayWorkspace: async () => undefined,
      deleteSession: async () => undefined, renameSession: async () => undefined,
      forkSessionContext: async () => undefined, setSessionParent: async () => undefined,
    });
    return null;
  }
  let renderer!: ReactTestRenderer;
  await act(async () => { renderer = create(<Harness />); await flushEffects(); });
  cleanup.push(() => act(() => renderer.unmount()));
  const handle = await runtime.openWorkspace(TEST_WORKSPACE_REQUEST_TARGET, access);
  return {
    explorer: () => explorer, actions: () => actions, handle, receipts, nodes, errors, statuses, childQueries,
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
          created_node_id: failed ? null : nodeId,
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
        await flushEffects();
      });
    },
  };
}

describe("未提交父 Folder 的两条真实 Web 入口", () => {
  for (const entry of ["explorer", "shell"] as const) {
    test(`${entry} 保留创建依赖、本地父子投影和提交后的真实父 ID`, async () => {
      const mounted = await mountPendingParent();
      const createFolder = (name: string, parent: string | null) => entry === "explorer"
        ? mounted.explorer().createSessionFolder("ws-test", name, parent)
        : mounted.actions().createSessionFolder("ws-test", parent, name);
      await act(async () => { await createFolder("父目录", null); });
      const parent = mounted.handle.driver.current().operations[0]!;
      await act(async () => { await createFolder("子目录", parent.client_ref); });
      const child = mounted.handle.driver.current().operations[1]!;
      expect(child.created_by_operation_id).toBe(parent.client_operation_id);
      expect(child.depends_on).toEqual([parent.client_operation_id]);
      expect(child.parent_node_id).toBeNull();
      expect(mounted.explorer().branches.get("ws-test:root")?.total).toBe(0);
      const local = mounted.explorer().branches.get(`ws-test:${parent.client_ref}`)!;
      expect(local.local_pending).toBe(true);
      expect(local.catalog_revision).toBeUndefined();
      expect(local.revision).toBe("");
      expect(local.total).toBeNull();
      expect(local.items.map((item) => [item.name, item.parent_node_id, item.pending_state]))
        .toEqual([["子目录", parent.client_ref, "accepted"]]);
      await act(async () => { await mounted.explorer().loadBranch("ws-test", parent.client_ref); });
      expect(mounted.childQueries).not.toContain(parent.client_ref);
      if (entry === "shell") expect(mounted.statuses).toEqual([
        "会话文件夹已持久受理，等待提交", "会话文件夹已持久受理，等待提交",
      ]);
      await mounted.settle();
      const parentId = "fld_00000000000000000000000000000001";
      expect(mounted.childQueries).toContain(parentId);
      expect(mounted.explorer().branches.get(`ws-test:${parentId}`)?.items[0]?.name).toBe("子目录");
      expect(mounted.explorer().branches.has(`ws-test:${parent.client_ref}`)).toBe(false);
      expect(mounted.errors).toEqual([]);
    });
  }

  test("父创建失败时子项终结，权威重读保留独立成功目录", async () => {
    const mounted = await mountPendingParent();
    await act(async () => { await mounted.explorer().createSessionFolder("ws-test", "父目录"); });
    const parent = mounted.handle.driver.current().operations[0]!;
    await act(async () => {
      await mounted.actions().createSessionFolder("ws-test", parent.client_ref, "子目录");
      await mounted.explorer().createSessionFolder("ws-test", "独立目录");
    });
    await mounted.settle(true);
    expect(mounted.explorer().branches.get("ws-test:root")?.items.map((item) => item.name))
      .toEqual(["独立目录"]);
    expect(mounted.explorer().branches.has(`ws-test:${parent.client_ref}`)).toBe(false);
    expect(mounted.errors.some((error) => error.includes("父目录创建被明确拒绝"))).toBe(true);
    expect([...mounted.receipts.values()].map((receipt) => receipt.state))
      .toEqual(["rejected", "dependency_failed", "committed"]);
    expect(mounted.childQueries).not.toContain(parent.client_ref);
  });
});

import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { act } from "react-test-renderer";
import type {
  SessionCatalogNavigationEvent,
  SessionCatalogNavigationEventsPage,
  SessionCatalogOperationReceipt,
  SessionCatalogOperationStatusPage,
} from "../../api/session/sessionCatalogOperations";
import {
  addCatalogOutboxIntent,
  applyCatalogOutboxOperationReceipt,
  assignCatalogOutboxOperationSequence,
  catalogOutboxPartitionKey,
  createCatalogOutbox,
  markCatalogOutboxOperationPersisted,
  type CatalogOutboxPartition,
} from "../../state/session/sessionCatalogOutbox";
import type {
  CatalogOutboxPersistencePort,
  CatalogOutboxStoredOperation,
} from "../../state/session/sessionCatalogOutboxStore";
import type { GatewayUserAccess, Session, SessionCatalogNode } from "../../types/backend";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import {
  apiSuccessResponse,
  catalogChildrenResponse,
  apiErrorResponse,
  installGatewayFetch,
  restoreGatewayTestEnvironment,
  testGatewayUserAccess,
  withDefaultGatewayApiResponses,
} from "../../tests/fixtures/session/gatewayEnvironment";
import {
  createSessionHookState,
  createAppStateMirror,
} from "../../tests/fixtures/session/appState";
import { createDeferredResult, flushReactEffects } from "../../tests/fixtures/session/asyncControl";
import {
  mountSessionResourceExplorerHarness,
} from "../../tests/fixtures/session/sessionResourceExplorer";
import {
  catalogOutboxWorkspaceScopeKey,
  createSessionCatalogOutboxRuntime,
} from "./sessionCatalogOutboxRuntime";
import type { SessionCatalogOperationsAdapter } from "./sessionCatalogOutboxDriver";
import { refreshCurrentSessionCatalogOwner } from "./useSessionLifecycleActions";
import { CATALOG_OUTBOX_BROADCAST_CHANNEL } from "./sessionCatalogOutboxDriver";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { CatalogReadSupersededError } from "../sessionResourceExplorer/resourceTreeSync";

const API_PORT = 49_441;
const GATEWAY_ID = "gateway-test";
const WORKSPACE_ID = "c6dd923e-4a7c-4db3-8c63-110ea072d3a4";
const WORKSPACE_TARGET: GatewayWorkspaceRequestTarget = { gateway_id: "gateway-test", workspace_id: WORKSPACE_ID };
const GATEWAY_WORKSPACE_ID = gatewayWorkspaceKey(WORKSPACE_TARGET);
const OPERATION_ID = "op_00000000000000000000000000000001";

function navigationEvent(sequence: number): SessionCatalogNavigationEvent {
  return { event_seq: sequence, workspace_id: WORKSPACE_ID,
    operation_id: `op_${sequence.toString(16).padStart(32, "0")}`, queue_seq: sequence,
    kind: "rename_node", result_state: "committed", committed_catalog_revision: 7 + sequence,
    affected_node_ids: ["cnode_1"], error_code: null, error_detail: null,
    created_at: "2026-10-11T00:00:00Z" };
}

afterEach(restoreGatewayTestEnvironment);

function rejectedReceipt(): SessionCatalogOperationReceipt {
  return {
    operation_id: OPERATION_ID,
    client_sequence: 1,
    queue_seq: 1,
    kind: "rename_node",
    state: "rejected",
    created_node_id: null,
    committed_catalog_revision: null,
    error_code: "session_busy",
    error_detail: "会话正在运行",
    settlement: null,
    receipt_revision: 1,
    updated_at: "2026-10-04T00:00:00Z",
  };
}

function persistedOperation(partition: CatalogOutboxPartition = {
  gatewayId: GATEWAY_ID,
  workspaceId: WORKSPACE_ID,
  principal: "guest",
}, name = "新名字"): {
  record: CatalogOutboxStoredOperation;
} {
  const outbox = createCatalogOutbox(partition);
  const pending = addCatalogOutboxIntent(
    outbox,
    OPERATION_ID,
    { kind: "rename_node", targetNodeId: "cnode_1", name },
    { baseCatalogRevision: 7, expectedRevision: 3 },
  );
  const sequenced = assignCatalogOutboxOperationSequence(pending, OPERATION_ID, 1);
  const persisted = markCatalogOutboxOperationPersisted(sequenced, OPERATION_ID);
  const operation = persisted.operations[0];
  if (!operation) throw new Error("测试 outbox 没有生成持久化命令");
  const partitionKey = catalogOutboxPartitionKey(partition);
  return {
    record: {
      partition_key: partitionKey,
      client_sequence: 1,
      operation: { ...operation, client_sequence: 1 },
    },
  };
}

function repairDeleteFixture(refreshFails: boolean) {
  const detail = "日期目录隔离失败：Permission denied";
  const refreshFailure = new Error("父分支刷新失败");
  const folder: SessionCatalogNode = {
    node_id: "fld_repair", folder_id: "fld_repair", kind: "folder", name: "待删除目录",
    parent_node_id: null, has_children: false, node_revision: 2, state: "active",
  };
  const records = new Map<string, CatalogOutboxStoredOperation>();
  const receipts = new Map<string, SessionCatalogOperationReceipt>();
  const deleted: string[] = [];
  const enqueued: string[] = [];
  const errors: string[] = [];
  const phases: string[] = [];
  const accepted = createDeferredResult<void>();
  let sequence = 0;
  let catalogRevision = 3;
  let releaseRepair!: () => void;
  const repairGate = new Promise<void>((resolve) => { releaseRepair = resolve; });
  let queryStarted!: () => void;
  const queried = new Promise<void>((resolve) => { queryStarted = resolve; });
  const persistence: CatalogOutboxPersistencePort = {
    load: async (key) => [...records.values()].filter((record) => record.partition_key === key),
    insert: async (key, operation) => {
      const clientSequence = ++sequence;
      const record: CatalogOutboxStoredOperation = {
        partition_key: key, client_sequence: clientSequence,
        operation: { ...operation, client_sequence: clientSequence },
      };
      records.set(operation.client_operation_id, record);
      return record;
    },
    applyReceipts: async (key, updates) => {
      const changed: CatalogOutboxStoredOperation[] = [];
      for (const receipt of updates) {
        const previous = records.get(receipt.operation_id);
        if (!previous || previous.partition_key !== key) continue;
        const record = { ...previous, operation: {
          ...applyCatalogOutboxOperationReceipt(previous.operation, receipt),
          client_sequence: previous.client_sequence,
        } };
        records.set(receipt.operation_id, record);
        changed.push(record);
      }
      return changed;
    },
    delete: async (_key, ids) => {
      deleted.push(...ids);
      for (const id of ids) records.delete(id);
    },
  };
  installGatewayFetch(withDefaultGatewayApiResponses(({ path, url }) => {
    if (path === "/api/gateway/health") return apiSuccessResponse({ gateway_id: GATEWAY_ID });
    if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
      workspace_id: WORKSPACE_ID, catalog_revision: catalogRevision,
      event_seq_watermark: 0, generation: 1,
    });
    if (path === "/api/v1/session-catalog/children") {
      if (catalogRevision === 4 && refreshFails) throw refreshFailure;
      const parent = new URL(url).searchParams.get("parent_node_id");
      return catalogChildrenResponse(`catalog-${catalogRevision}`,
        catalogRevision === 3 && parent === null ? [folder] : [], parent, catalogRevision);
    }
    return undefined;
  }));
  const runtimeOptions: Parameters<typeof createSessionCatalogOutboxRuntime>[0] = {
    apiPort: API_PORT, reconcileIntervalMs: 60_000,
    openPersistence: async () => persistence,
    onError: (message) => errors.push(message),
    adapter: {
      enqueue: async (_port, target, intents) => {
        expect(target).toEqual(WORKSPACE_TARGET);
        const accepted = intents.map((intent): SessionCatalogOperationReceipt => ({
          operation_id: intent.client_operation_id, client_sequence: intent.client_sequence,
          queue_seq: intent.client_sequence, kind: intent.kind, state: "queued",
          created_node_id: null, committed_catalog_revision: null, error_code: null,
          error_detail: null, settlement: null, receipt_revision: 1,
          updated_at: "2026-10-14T00:00:00Z",
        }));
        for (const receipt of accepted) {
          enqueued.push(receipt.operation_id);
          receipts.set(receipt.operation_id, receipt);
        }
        return { workspace_id: WORKSPACE_ID, accepted_count: accepted.length,
          receipts: accepted, created_node_ids: {} };
      },
      queryStatus: async (_port, target, ids) => {
        expect(target).toEqual(WORKSPACE_TARGET);
        queryStarted();
        await repairGate;
        catalogRevision = 4;
        return { workspace_id: WORKSPACE_ID, catalog_revision: catalogRevision,
          items: ids.map((id): SessionCatalogOperationReceipt => {
            const receipt = receipts.get(id);
            if (!receipt) throw new Error(`测试未受理待核对的删除: ${id}`);
            return { ...receipt, state: "committed", committed_catalog_revision: 4,
              settlement: "repair_required", receipt_revision: 2,
              error_code: "delete_settlement_repair_required", error_detail: detail };
          }), unknown_operation_ids: [] };
      },
    },
  };
  const runtime = createSessionCatalogOutboxRuntime(runtimeOptions);
  runtime.activatePrincipal(testGatewayUserAccess());
  runtime.subscribe(() => {
    for (const entry of runtime.getSnapshot().values()) {
      phases.push(...entry.outbox.operations.map((operation) =>
        `${operation.state}:${operation.server_state ?? "none"}:${operation.settlement ?? "none"}`));
      if (entry.outbox.operations.some((operation) => operation.state === "accepted")) accepted.release();
    }
  });
  return { runtime, records, deleted, enqueued, errors, phases, folder, detail,
    queried, releaseRepair, refreshFailure, runtimeOptions, accepted: accepted.promise };
}

describe("真实 runtime 的删除修复提示", () => {
  test("首次公开 submit 与空回执启动对账并发时保留精确持久操作", async () => {
    const fixture = repairDeleteFixture(false);
    try {
      fixture.runtime.activatePrincipal(testGatewayUserAccess());
      // 首次业务入口直接 submit；不预开 workspace，也不等待启动对账。
      const submitted = await fixture.runtime.submit(WORKSPACE_TARGET, testGatewayUserAccess(), {
        kind: "rename_node", targetNodeId: fixture.folder.node_id, name: "首次提交",
      }, { baseCatalogRevision: 3, expectedRevision: 2 });

      await fixture.accepted;
      expect(fixture.enqueued).toEqual([submitted.operationId]);
      expect(fixture.records.get(submitted.operationId)).toMatchObject({
        operation: {
          client_operation_id: submitted.operationId,
          state: "accepted",
          server_state: "queued",
          receipt_revision: 1,
        },
      });
      expect(submitted.handle.driver.current().operations).toMatchObject([{
        client_operation_id: submitted.operationId,
        state: "accepted",
        receipt_revision: 1,
      }]);
    } finally {
      fixture.runtime.dispose();
      fixture.releaseRepair();
    }
  });

  for (const refreshFails of [false, true]) {
    test(`Folder repair 回执与${refreshFails ? "失败" : "成功"}刷新均保留同操作的排空错误`, async () => {
      const fixture = repairDeleteFixture(refreshFails);
      let mounted: Awaited<ReturnType<typeof mountSessionResourceExplorerHarness>> | undefined;
      try {
        mounted = await mountSessionResourceExplorerHarness({ props: {
          apiPort: API_PORT, activeWorkspaceId: GATEWAY_WORKSPACE_ID, workspaces: [WORKSPACE_TARGET],
          gatewayUserAccess: testGatewayUserAccess(), sessionCatalogOutboxRuntime: fixture.runtime,
        } });
        let result!: Awaited<ReturnType<ReturnType<typeof mounted.explorer>["deleteSessionFolder"]>>;
        await act(async () => {
          result = await mounted!.explorer().deleteSessionFolder(GATEWAY_WORKSPACE_ID, fixture.folder.node_id);
        });
        await fixture.queried;
        await act(async () => {
          fixture.releaseRepair();
          await expect(result.completion).rejects.toThrow(fixture.detail);
          await flushReactEffects();
        });
        expect(fixture.enqueued).toEqual([result.operationId]);
        expect(fixture.phases).toContain("accepted:queued:none");
        expect(fixture.phases).toContain("committed:committed:repair_required");
        expect(fixture.deleted).toEqual([]);
        expect(fixture.records.get(result.operationId)?.operation).toMatchObject({
          client_operation_id: result.operationId, state: "committed", settlement: "repair_required",
          error_detail: fixture.detail, receipt_revision: 2,
        });
        const firstRepair = fixture.errors.find((message) => message.includes(fixture.detail));
        expect(firstRepair).toContain(result.operationId);
        expect(firstRepair).toContain("删除已提交但排空失败");
        const finalError = fixture.errors[fixture.errors.length - 1];
        expect(finalError).toContain(result.operationId);
        expect(finalError).toContain("删除已提交但排空失败，需修复");
        expect(finalError).toContain(fixture.detail);
        if (refreshFails) expect(finalError).toContain(fixture.refreshFailure.message);
        else expect(finalError).not.toContain("目录分支刷新失败");
      } finally {
        mounted?.unmount();
        fixture.releaseRepair();
        fixture.runtime.dispose();
      }
    });
  }

  test("恢复的未跟踪 repair 操作在分支刷新失败时仍报告原身份与后端排空详情", async () => {
    const fixture = repairDeleteFixture(true);
    let mounted: Awaited<ReturnType<typeof mountSessionResourceExplorerHarness>> | undefined;
    let recovered: ReturnType<typeof createSessionCatalogOutboxRuntime> | undefined;
    try {
      const initial = await fixture.runtime.openWorkspace(WORKSPACE_TARGET, testGatewayUserAccess());
      await initial.driver.reconcile();
      const submitted = await fixture.runtime.submit(WORKSPACE_TARGET, testGatewayUserAccess(), {
        kind: "delete_folder", targetNodeId: fixture.folder.node_id, recursive: true,
      }, { baseCatalogRevision: 3, expectedRevision: 2 });
      const terminal = fixture.runtime.waitForTerminal(submitted.handle, submitted.operationId);
      await fixture.queried;
      fixture.releaseRepair();
      await expect(terminal).rejects.toThrow(fixture.detail);
      await expect(terminal).rejects.toHaveProperty("name", "ReportedError");
      expect(fixture.errors.some((message) => message.includes(fixture.detail))).toBe(true);
      fixture.runtime.dispose();
      recovered = createSessionCatalogOutboxRuntime(fixture.runtimeOptions);
      recovered.activatePrincipal(testGatewayUserAccess());
      const restored = await recovered.openWorkspace(WORKSPACE_TARGET, testGatewayUserAccess());
      expect(restored.driver.current().operations).toMatchObject([{
        client_operation_id: submitted.operationId, state: "committed", settlement: "repair_required",
        error_detail: fixture.detail, receipt_revision: 2,
      }]);
      const previousErrors = fixture.errors.length;
      mounted = await mountSessionResourceExplorerHarness({ props: {
        apiPort: API_PORT, activeWorkspaceId: GATEWAY_WORKSPACE_ID, workspaces: [WORKSPACE_TARGET],
        gatewayUserAccess: testGatewayUserAccess(), sessionCatalogOutboxRuntime: recovered,
      } });
      const restoredError = fixture.errors.slice(previousErrors).find((message) =>
        message.includes("终态分支刷新失败"));
      expect(restoredError).toContain(submitted.operationId);
      expect(restoredError).toContain("删除已提交但排空失败，需修复");
      expect(restoredError).toContain(fixture.detail);
      expect(restoredError).toContain(fixture.refreshFailure.message);
      expect(fixture.deleted).toEqual([]);
      expect(fixture.records.get(submitted.operationId)?.operation.settlement).toBe("repair_required");
      expect(fixture.enqueued).toEqual([submitted.operationId]);
    } finally {
      mounted?.unmount();
      fixture.releaseRepair();
      recovered?.dispose();
      fixture.runtime.dispose();
    }
  });
});

describe("会话目录 outbox runtime", () => {
  test("直接使用已选 owner，零工作区身份 GET 且同 UUID 的不同 Gateway 分区可分别恢复", async () => {
    const partitions: CatalogOutboxPartition[] = [
      { gatewayId: GATEWAY_ID, workspaceId: WORKSPACE_ID, principal: "guest" },
      { gatewayId: "gateway-other", workspaceId: WORKSPACE_ID, principal: "guest" },
    ];
    const targets = partitions.map((partition) => ({
      gateway_id: partition.gatewayId, workspace_id: partition.workspaceId,
    }));
    const queued: SessionCatalogOperationReceipt = {
      ...rejectedReceipt(), state: "queued", error_code: null, error_detail: null,
    };
    const records = partitions.map((partition, index) => {
      const { record } = persistedOperation(partition, `既有命令 ${index}`);
      return { ...record, operation: { ...applyCatalogOutboxOperationReceipt(record.operation, queued), client_sequence: record.client_sequence } };
    });
    const loads: string[] = [];
    let identityRequests = 0;
    installGatewayFetch(({ path, method }) => {
      if (method === "GET" && (path === "/api/v1/workspace" || /^\/api\/v1\/workspaces\/[^/]+\/workspace$/.test(path))) {
        identityRequests += 1;
        return apiSuccessResponse({ workspace_id: WORKSPACE_ID });
      }
      return undefined;
    });
    const options: Parameters<typeof createSessionCatalogOutboxRuntime>[0] = {
      apiPort: API_PORT, reconcileIntervalMs: 60_000,
      openPersistence: async () => ({
        async load(key) { loads.push(key); return records.filter((record) => record.partition_key === key); },
        insert: async () => { throw new Error("打开既有分区不得新增命令"); },
        applyReceipts: async (_key, receipts) => { expect(receipts).toEqual([]); return []; },
        delete: async () => { throw new Error("打开既有分区不得删除命令"); },
      }),
      adapter: {
        enqueue: async () => { throw new Error("打开既有分区不得重新入队"); },
        queryStatus: async (_port, target, ids) => {
          expect(targets).toContainEqual(target);
          expect(ids).toEqual([OPERATION_ID]);
          return { workspace_id: WORKSPACE_ID, catalog_revision: 7, items: [], unknown_operation_ids: [] };
        },
      },
    };
    const runtimes: ReturnType<typeof createSessionCatalogOutboxRuntime>[] = [];
    try {
      // 同一持久端口先打开、再重建 runtime，原三元组键和命令均保持不变。
      for (let restore = 0; restore < 2; restore += 1) {
        const runtime = createSessionCatalogOutboxRuntime(options);
        runtimes.push(runtime);
        const access = testGatewayUserAccess();
        runtime.activatePrincipal(access);
        const handles = await Promise.all(targets.map((target) => runtime.openWorkspace(target, access)));
        await Promise.all(handles.map((handle) => handle.driver.reconcile()));
        for (const [index, handle] of handles.entries()) {
          expect(handle.workspaceTarget).toEqual(targets[index]);
          expect(handle.partition).toEqual(partitions[index]);
          expect(handle.partitionKey).toBe(records[index]!.partition_key);
          expect(handle.driver.current().operations).toEqual([records[index]!.operation]);
        }
        expect(handles[0]!.driver).not.toBe(handles[1]!.driver);
        expect(runtime.getSnapshot().size).toBe(2);
        runtime.dispose();
      }
      expect(loads).toEqual(records.map((record) => record.partition_key).concat(records.map((record) => record.partition_key)));
      expect(identityRequests).toBe(0);
    } finally { for (const runtime of runtimes) runtime.dispose(); }
  });

  test("持久层打开和 driver 恢复期间 principal 离开再返回，迟到 opening 不得取代新 generation", async () => {
    const access = testGatewayUserAccess();
    const otherAccess: GatewayUserAccess = { ...access, kind: "user", user_id: "another-user" };
    const opened = createDeferredResult<void>();
    const persistenceGate = createDeferredResult<CatalogOutboxPersistencePort>();
    const restoring = createDeferredResult<void>();
    const restoreGate = createDeferredResult<void>();
    let loads = 0;
    const persistence: CatalogOutboxPersistencePort = {
      async load() {
        loads += 1;
        if (loads === 1) { restoring.release(); await restoreGate.promise; }
        return [];
      },
      insert: async () => { throw new Error("打开分区不得新增命令"); },
      applyReceipts: async (_key, receipts) => { expect(receipts).toEqual([]); return []; },
      delete: async () => { throw new Error("打开分区不得删除命令"); },
    };
    installGatewayFetch(() => { throw new Error("打开分区不得发送身份 HTTP 请求"); });
    const runtime = createSessionCatalogOutboxRuntime({
      apiPort: API_PORT, reconcileIntervalMs: 60_000,
      openPersistence: () => { opened.release(); return persistenceGate.promise; },
      adapter: {
        enqueue: async () => { throw new Error("空分区不得入队"); },
        queryStatus: async () => { throw new Error("空分区不得查询命令"); },
      },
    });
    try {
      runtime.activatePrincipal(access);
      const opening = runtime.openWorkspace(WORKSPACE_TARGET, access).catch((error: unknown) => error);
      await opened.promise;
      runtime.activatePrincipal(otherAccess);
      runtime.activatePrincipal(access);
      const restore = runtime.openWorkspace(WORKSPACE_TARGET, access).catch((error: unknown) => error);
      persistenceGate.release(persistence);
      expect(await opening).toHaveProperty("message", "Gateway 认证主体已切换，拒绝继续使用会话目录 outbox: guest");
      await restoring.promise;
      runtime.activatePrincipal(otherAccess);
      runtime.activatePrincipal(access);
      const current = await runtime.openWorkspace(WORKSPACE_TARGET, access);
      await current.driver.reconcile();
      const snapshot = runtime.getSnapshot();
      restoreGate.release();
      expect(await restore).toHaveProperty("message", `会话目录 outbox principal 已失效，停止派发: ${catalogOutboxPartitionKey({
        gatewayId: GATEWAY_ID, workspaceId: WORKSPACE_ID, principal: "guest",
      })}`);
      expect(loads).toBe(2);
      expect(runtime.getSnapshot()).toBe(snapshot);
      expect(await runtime.openWorkspace(WORKSPACE_TARGET, access)).toBe(current);
      expect(current.driver.current().operations).toEqual([]);
    } finally {
      persistenceGate.release(persistence);
      restoreGate.release();
      runtime.dispose();
    }
  });

  for (const reloadFails of [false, true]) {
    test(`恢复期间收到跨窗口广播，恢复后重读${reloadFails ? "失败仍报告真实错误" : "取得最新磁盘命令"}`, async () => {
      installGatewayFetch(({ path }) => {
        if (path === "/api/gateway/health") return apiSuccessResponse({ gateway_id: GATEWAY_ID });
        return undefined;
      });
      let started!: () => void;
      const loading = new Promise<void>(resolve => { started = resolve; });
      let release!: () => void;
      const restoreGate = new Promise<void>(resolve => { release = resolve; });
      let observed!: (value: string) => void;
      const outcome = new Promise<string>(resolve => { observed = resolve; });
      const { record } = persistedOperation();
      const errors: string[] = [];
      let loads = 0;
      const runtime = createSessionCatalogOutboxRuntime({
        apiPort: API_PORT, reconcileIntervalMs: 60_000,
        onError(message) { errors.push(message); observed("error"); },
        openPersistence: async () => ({
          async load() {
            loads += 1;
            if (loads === 1) { started(); await restoreGate; return []; }
            if (reloadFails) throw new Error("IndexedDB 重读失败");
            return [record];
          },
          insert: async () => { throw new Error("广播不得新增命令"); },
          applyReceipts: async () => [],
          delete: async () => { throw new Error("广播不得删除命令"); },
        }),
        adapter: {
          enqueue: async () => { throw new Error("广播不得提交命令"); },
          queryStatus: async () => { throw new Error("本地 pending 不应查询回执"); },
        },
      });
      const sender = new BroadcastChannel(CATALOG_OUTBOX_BROADCAST_CHANNEL);
      let delivered!: () => void;
      const received = new Promise<void>(resolve => { delivered = resolve; });
      const addListener = BroadcastChannel.prototype.addEventListener;
      // 在真实接收者执行监听后确认投递，不以另一个通道收到消息推断它已处理。
      const listenerSpy = spyOn(BroadcastChannel.prototype, "addEventListener").mockImplementation(function (
        this: BroadcastChannel, type: string, listener: EventListenerOrEventListenerObject | null,
        options?: boolean | AddEventListenerOptions,
      ) {
        addListener.call(this, type, (event: Event) => {
          if (typeof listener === "function") listener.call(this, event);
          else listener?.handleEvent(event);
          delivered();
        }, options);
      });
      const unsubscribe = runtime.subscribe(() => {
        const entry = [...runtime.getSnapshot().values()][0];
        if (entry?.outbox.operations.length) observed("reloaded");
      });
      let opening: ReturnType<typeof runtime.openWorkspace> | undefined;
      try {
        const access = testGatewayUserAccess();
        runtime.activatePrincipal(access);
        opening = runtime.openWorkspace(WORKSPACE_TARGET, access);
        await loading;
        sender.postMessage({ partitionKey: record.partition_key });
        await received;
        expect(loads).toBe(1);
        expect(errors).toEqual([]);
        release();
        const handle = await opening;
        expect(await outcome).toBe(reloadFails ? "error" : "reloaded");
        expect(loads).toBe(2);
        if (reloadFails) {
          expect(errors).toEqual([`重新读取会话目录本地 outbox 失败 (${GATEWAY_WORKSPACE_ID}): IndexedDB 重读失败`]);
          expect(handle.driver.current().operations).toEqual([]);
        } else {
          expect(errors).toEqual([]);
          expect(handle.driver.current().operations).toEqual([record.operation]);
        }
      } finally {
        release();
        try { await opening; }
        finally {
          unsubscribe();
          sender.close();
          runtime.dispose();
          listenerSpy.mockRestore();
        }
      }
    });
  }

  test("恢复的拒绝回执先经过稳定错误通道，再快速清理 outbox", async () => {
    const access: GatewayUserAccess = testGatewayUserAccess();
    const { record } = persistedOperation();
    const errors: string[] = [];
    let errorsAtDelete: string[] = [];
    let statusRequested!: () => void;
    const statusRequest = new Promise<void>((resolve) => { statusRequested = resolve; });
    const queryTargets: GatewayWorkspaceRequestTarget[] = [];
    let allowStatus!: (page: SessionCatalogOperationStatusPage) => void;
    const statusResponse = new Promise<SessionCatalogOperationStatusPage>((resolve) => {
      allowStatus = resolve;
    });
    const persistence: CatalogOutboxPersistencePort = {
      async load(partitionKey) {
        return partitionKey === record.partition_key ? [record] : [];
      },
      async insert() {
        throw new Error("恢复对账用例不应新增 outbox 命令");
      },
      async applyReceipts(partitionKey, receipts) {
        if (partitionKey !== record.partition_key) return [];
        for (const receipt of receipts) {
          if (receipt.operation_id !== record.operation.client_operation_id) continue;
          record.operation = { ...applyCatalogOutboxOperationReceipt(record.operation, receipt),
            client_sequence: record.client_sequence };
        }
        return [record];
      },
      async delete() {
        errorsAtDelete = [...errors];
      },
    };
    const adapter: SessionCatalogOperationsAdapter = {
      async enqueue() {
        throw new Error("恢复对账用例不应重新入队");
      },
      async queryStatus(_port, workspaceTarget, operationIds, signal) {
        queryTargets.push(workspaceTarget);
        expect(operationIds).toEqual([OPERATION_ID]);
        expect(signal?.aborted).toBe(false);
        statusRequested();
        return await statusResponse;
      },
    };
    installGatewayFetch(({ path }) => {
      if (path === "/api/gateway/health") {
        return apiSuccessResponse({ gateway_id: GATEWAY_ID });
      }
      if (path === "/api/v1/session-catalog/snapshot") {
        return apiSuccessResponse({ workspace_id: WORKSPACE_ID,
          catalog_revision: 7, event_seq_watermark: 0, generation: 1 });
      }
      if (path === "/api/v1/session-catalog/navigation-events") {
        return apiSuccessResponse({ workspace_id: WORKSPACE_ID, event_seq_watermark: 0,
          items: [], next_cursor: null, has_more: false, cursor_gone: false });
      }
      return undefined;
    });
    const runtime = createSessionCatalogOutboxRuntime({
      apiPort: API_PORT,
      openPersistence: async () => persistence,
      adapter,
      onError: (message) => errors.push(message),
    });

    try {
      runtime.activatePrincipal(access);
      const phases: string[] = [];
      runtime.registerFailureOwner(access, {
        async refresh(target, reported, revision, signal) {
          expect(target).toEqual(WORKSPACE_TARGET);
          expect(revision).toBe(7);
          expect(reported.operations[0].state).toBe("rejected");
          expect(signal.aborted).toBe(false);
          phases.push("confirmed");
        },
        async waitForProjection() { phases.push("projection"); },
      });
      const handle = await runtime.openWorkspace(WORKSPACE_TARGET, access);
      await statusRequest;
      const reconciling = handle.driver.reconcile();
      allowStatus({
        workspace_id: WORKSPACE_ID,
        catalog_revision: 7,
        items: [rejectedReceipt()],
        unknown_operation_ids: [],
      });
      // 等待 driver 完成持久清理；删除回调记录当时已交付的错误。
      await reconciling;

      expect(phases).toEqual(["confirmed", "projection", "projection"]);
      expect(queryTargets).toEqual([WORKSPACE_TARGET]);
      expect(handle.workspaceTarget).toEqual(WORKSPACE_TARGET);
      expect(errorsAtDelete).toEqual(errors);
      expect(errors.join("\n")).toContain(OPERATION_ID);
      expect(errors.join("\n")).toContain("会话正在运行");
      expect(handle.driver.current().operations).toEqual([]);
      expect(runtime.getSnapshot().get(
        catalogOutboxWorkspaceScopeKey(GATEWAY_WORKSPACE_ID, "guest"),
      )?.outbox.operations).toEqual([]);
    } finally {
      runtime.dispose();
    }
  });
});

describe("失败 confirmed owner 的恢复边界", () => {
  for (const failure of ["owner 缺席", "刷新失败", "owner 在刷新中撤销"] as const) {
    test(`${failure}保留持久失败回执与真实错误，后注册 owner 可恢复`, async () => {
      const access = testGatewayUserAccess();
      const { record } = persistedOperation();
      let records = [record];
      const errors: string[] = [];
      const phases: string[] = [];
      let completeDelete!: () => void;
      const deleted = new Promise<void>((resolve) => { completeDelete = resolve; });
      let refreshStarted!: () => void;
      const refreshing = new Promise<void>((resolve) => { refreshStarted = resolve; });
      let finishRefresh!: () => void;
      const refreshGate = new Promise<void>((resolve) => { finishRefresh = resolve; });
      let queryCount = 0;
      installGatewayFetch(({ path }) => {
        if (path === "/api/gateway/health") return apiSuccessResponse({ gateway_id: GATEWAY_ID });
        if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
          workspace_id: WORKSPACE_ID, catalog_revision: 12, event_seq_watermark: 0, generation: 1,
        });
        if (path === "/api/v1/session-catalog/navigation-events") return apiSuccessResponse({
          workspace_id: WORKSPACE_ID, event_seq_watermark: 0, items: [],
          next_cursor: null, has_more: false, cursor_gone: false,
        });
        return undefined;
      });
      const runtime = createSessionCatalogOutboxRuntime({ apiPort: API_PORT, reconcileIntervalMs: 60_000,
        onError: (message) => errors.push(message),
        openPersistence: async () => ({
          load: async () => records,
          insert: async () => { throw new Error("恢复用例不应新增命令"); },
          applyReceipts: async (_key, receipts) => {
            for (const receipt of receipts) {
              records = records.map((record) => record.operation.client_operation_id === receipt.operation_id
                ? { ...record, operation: {
                  ...applyCatalogOutboxOperationReceipt(record.operation, receipt),
                  client_sequence: record.client_sequence,
                } } : record);
            }
            return records.filter((record) => receipts.some((receipt) =>
              receipt.operation_id === record.operation.client_operation_id));
          },
          delete: async () => { phases.push("delete"); records = []; completeDelete(); },
        }),
        adapter: {
          enqueue: async () => { throw new Error("拒绝命令不得重复入队"); },
          queryStatus: async (_port, _target, ids) => {
            queryCount += 1;
            expect(ids).toEqual([OPERATION_ID]);
            return { workspace_id: WORKSPACE_ID, catalog_revision: 12,
              items: [rejectedReceipt()], unknown_operation_ids: [] };
          },
        },
      });
      try {
        runtime.activatePrincipal(access);
        let unregister: () => void = () => undefined;
        if (failure !== "owner 缺席") {
          unregister = runtime.registerFailureOwner(access, {
            async refresh() {
              if (failure === "刷新失败") throw new Error("权威 snapshot 请求失败");
              refreshStarted();
              await refreshGate;
            },
            async waitForProjection() {},
          });
        }
        const handle = await runtime.openWorkspace(WORKSPACE_TARGET, access);
        const attempt = handle.driver.reconcile();
        if (failure === "owner 在刷新中撤销") {
          await refreshing;
          unregister();
          finishRefresh();
        }
        await expect(attempt).rejects.toThrow(failure === "刷新失败" ? "权威 snapshot 请求失败" : "owner");
        if (failure === "刷新失败") unregister();
        expect(handle.driver.current().operations.map((operation) => operation.client_operation_id)).toEqual([OPERATION_ID]);
        expect(records).toEqual([{ ...record, operation: {
          ...record.operation, state: "rejected", server_state: "rejected", queue_seq: 1,
          receipt_revision: 1, error_code: "session_busy", error_detail: "会话正在运行",
        } }]);
        expect(errors.join("；")).toContain("会话正在运行");
        expect(errors.join("；")).toContain(OPERATION_ID);
        runtime.registerFailureOwner(access, {
          async refresh(target, reported, revision, signal) {
            expect(target).toEqual(WORKSPACE_TARGET);
            expect(revision).toBe(12);
            expect(reported.operations[0].state).toBe("rejected");
            expect(signal.aborted).toBe(false);
            phases.push("refresh");
          },
          async waitForProjection() { phases.push("projection"); },
        });
        await deleted;
        await handle.driver.reconcile();
        expect(phases).toEqual(["refresh", "projection", "delete", "projection"]);
        expect(queryCount).toBe(1);
        expect(handle.driver.current().operations).toEqual([]);
      } finally { finishRefresh(); runtime.dispose(); }
    });
  }
});

describe("navigation 事件的真实 runtime 消费", () => {
  for (const mode of ["乱序重复", "事件缺口", "cursor gone", "分页", "刷新取代", "真实同文案错误"] as const) {
    test(`${mode}只使 confirmed 失效并读取最新 snapshot，不制造目录命令`, async () => {
      const errors: string[] = [];
      const refreshed: number[] = [];
      const requestedAfter: number[] = [];
      let interruptedAt: number | null = null;
      let watermark = 0;
      let ready!: () => void;
      const initialized = new Promise<void>((resolve) => { ready = resolve; });
      let completed!: () => void;
      const reconciled = new Promise<void>((resolve) => { completed = resolve; });
      let afterRefresh!: () => void;
      const resumed = new Promise<void>((resolve) => { afterRefresh = resolve; });
      installGatewayFetch(({ path, url, init }) => {
        if (path === "/api/gateway/health") return apiSuccessResponse({ gateway_id: GATEWAY_ID });
        if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
          workspace_id: WORKSPACE_ID, catalog_revision: 7 + watermark,
          event_seq_watermark: watermark, generation: 1,
        });
        if (path === "/api/v1/session-catalog/navigation-events") {
          expect(init?.signal?.aborted).toBe(false);
          const query = new URL(url).searchParams;
          const after = Number(query.get("after"));
          requestedAfter.push(after);
          if (after === 2) afterRefresh();
          const page: SessionCatalogNavigationEventsPage = {
            workspace_id: WORKSPACE_ID, event_seq_watermark: watermark,
            items: [], next_cursor: null, has_more: false, cursor_gone: false,
          };
          if (watermark === 2 && after < 2) {
            if (["乱序重复", "刷新取代", "真实同文案错误"].includes(mode)) page.items = [navigationEvent(2), navigationEvent(1), navigationEvent(1)];
            else if (mode === "事件缺口") page.items = [navigationEvent(2)];
            else if (mode === "cursor gone") page.cursor_gone = true;
            else if (query.get("cursor") === "page-two") page.items = [navigationEvent(2)];
            else { page.items = [navigationEvent(1)]; page.has_more = true; page.next_cursor = "page-two"; }
          }
          return apiSuccessResponse(page);
        }
        return undefined;
      });
      const runtime = createSessionCatalogOutboxRuntime({ apiPort: API_PORT, reconcileIntervalMs: 10,
        onError: (message) => errors.push(message),
        openPersistence: async () => ({ load: async () => [],
          insert: async () => { throw new Error("事件消费不得写入 outbox"); },
          applyReceipts: async (key, receipts) => {
            if (receipts.length > 0) throw new Error(`没有 mutation 不得持久合并回执: ${key}`);
            return [];
          },
          delete: async () => { throw new Error("没有 mutation 不得清理 outbox"); } }),
        adapter: {
          enqueue: async () => { throw new Error("事件消费不得重新入队"); },
          queryStatus: async () => { throw new Error("没有本地 operation 不得生成状态查询"); },
        },
      });
      try {
        const access = testGatewayUserAccess();
        runtime.activatePrincipal(access);
        runtime.registerFailureOwner(access, {
          async refresh(target, outbox, revision) {
            expect(target).toEqual(WORKSPACE_TARGET);
            expect(outbox.operations).toEqual([]);
            refreshed.push(revision);
            if (revision === 9 && interruptedAt === null && ["刷新取代", "真实同文案错误"].includes(mode)) {
              interruptedAt = requestedAfter.length;
              const message = "会话目录分支刷新已被更新请求取代";
              throw mode === "刷新取代" ? new CatalogReadSupersededError(message) : new Error(message);
            }
            if (revision === 7) ready();
            else if (revision === 9) completed();
          },
          async waitForProjection() {},
        });
        const handle = await runtime.openWorkspace(WORKSPACE_TARGET, access);
        await initialized;
        watermark = 2;
        await reconciled;
        await resumed;
        expect(refreshed).toEqual(interruptedAt === null ? [7, 9] : [7, 9, 9]);
        if (interruptedAt !== null) expect(requestedAfter[interruptedAt]).toBe(0);
        expect(requestedAfter).toContain(2);
        expect(handle.driver.current().operations).toEqual([]);
        if (mode === "真实同文案错误") {
          expect(errors).toHaveLength(1);
          expect(errors[0]).toContain("会话目录分支刷新已被更新请求取代");
        } else expect(errors).toEqual([]);
      } finally { runtime.dispose(); }
    });
  }
});

describe("navigation 对账中的已打开会话 owner", () => {
  const owner: Session = {
    session_id: "ses_catalog_owner", workspace_id: WORKSPACE_ID,
    resource_identity: "catalog-owner", uri: "boxteam://catalog-owner", scope: "workspace",
    title: "已打开 owner", title_source: "user", current_agent_id: "default", parent_session_id: null,
    created_at: "2026-10-13T00:00:00Z", updated_at: "2026-10-13T00:00:00Z",
  };
  // 已移出子树的会话可能仍保留旧 parent 镜像；owner 404 不能证明它已删除。
  const survivor: Session = {
    ...owner, session_id: "ses_survivor", title: "剩余会话", parent_session_id: owner.session_id,
  };
  const emptyAdapter: SessionCatalogOperationsAdapter = {
    enqueue: async () => { throw new Error("目录对账不得生成 mutation"); },
    queryStatus: async () => { throw new Error("没有本地 operation 不得查询回执"); },
  };
  const emptyPersistence: CatalogOutboxPersistencePort = {
    load: async () => [],
    insert: async () => { throw new Error("目录对账不得持久化 mutation"); },
    applyReceipts: async () => [],
    delete: async () => { throw new Error("目录对账不得删除 outbox"); },
  };

  test("同 event watermark 的结算版本仍经过 owner GET；pending保留，200整替，404只关闭已删除owner", async () => {
    const key = sessionScopeKey(GATEWAY_WORKSPACE_ID, owner.session_id);
    const mirror = createAppStateMirror({
      ...createSessionHookState({
        workspaceId: GATEWAY_WORKSPACE_ID, currentSession: owner, sessions: [owner, survivor], unreadSessionKeys: new Set([key]),
      }),
      apiPort: API_PORT, gatewayUserAccess: testGatewayUserAccess(),
    });
    let revision = 7;
    let status = 200;
    let listCalls = 0;
    let initialized!: () => void;
    const ready = new Promise<void>((resolve) => { initialized = resolve; });
    let failed!: () => void;
    const pendingError = new Promise<void>((resolve) => { failed = resolve; });
    let completed!: () => void;
    const removed = new Promise<void>((resolve) => { completed = resolve; });
    const errors: string[] = [];
    const fresh = { ...owner, title: "deleting owner 的完整 DTO", updated_at: "2026-10-13T01:00:00Z" };
    installGatewayFetch(({ path, init }) => {
      if (path === "/api/gateway/health") return apiSuccessResponse({ gateway_id: GATEWAY_ID });
      if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
        workspace_id: WORKSPACE_ID, catalog_revision: revision, event_seq_watermark: 0, generation: 1,
      });
      if (path === "/api/v1/session-catalog/navigation-events") return apiSuccessResponse({
        workspace_id: WORKSPACE_ID, event_seq_watermark: 0, items: [], next_cursor: null, has_more: false, cursor_gone: false,
      });
      if (path === "/api/v1/sessions") { listCalls += 1; throw new Error("owner失效不能重拉全量列表"); }
      if (path === `/api/v1/sessions/${owner.session_id}`) {
        expect(init?.signal?.aborted).toBe(false);
        if (status === 200) return apiSuccessResponse(fresh);
        const response = apiErrorResponse(status, status === 409 ? "会话日期目录正在隔离" : "Session not found");
        response.headers.set("X-Request-ID", `request-catalog-owner-${status}`);
        return response;
      }
      return undefined;
    });
    const runtime = createSessionCatalogOutboxRuntime({
      apiPort: API_PORT, reconcileIntervalMs: 10,
      openPersistence: async () => emptyPersistence, adapter: emptyAdapter,
      onError(message) { errors.push(message); failed(); },
      async refreshSessionOwner(target, signal) {
        await refreshCurrentSessionCatalogOwner(API_PORT, target, mirror.current, mirror.setState, signal);
        if (revision === 7) initialized();
        if (revision === 9) completed();
      },
    });
    try {
      const access = testGatewayUserAccess();
      runtime.activatePrincipal(access);
      await runtime.openWorkspace(WORKSPACE_TARGET, access);
      await ready;
      expect(mirror.current().currentSession).toEqual(fresh);
      expect(mirror.current().sessionsByWorkspace.get(GATEWAY_WORKSPACE_ID)?.[0]).toEqual(fresh);
      status = 409; revision = 8;
      await pendingError;
      expect(mirror.current().currentSession).toEqual(fresh);
      expect(errors.some((message) => message.includes("会话日期目录正在隔离"))).toBe(true);
      expect(errors).toContain(`对账会话目录操作失败 (${GATEWAY_WORKSPACE_ID}): 请求失败 409 : 会话日期目录正在隔离 [request_id=request-catalog-owner-409]`);
      status = 404; revision = 9;
      await removed;
      expect(mirror.current().currentSession).toBeNull();
      expect(mirror.current().currentSessionWorkspaceId).toBeNull();
      expect(mirror.current().sessions).toEqual([survivor]);
      expect(mirror.current().sessionsByWorkspace.get(GATEWAY_WORKSPACE_ID)).toEqual([survivor]);
      expect(mirror.current().unreadSessionKeys.has(key)).toBe(false);
      expect(listCalls).toBe(0);
    } finally { runtime.dispose(); }
  });

  test("周期对账在途时不重复订阅失败，后续独立请求仍报告并可恢复", async () => {
    const mirror = createAppStateMirror({
      ...createSessionHookState({ workspaceId: GATEWAY_WORKSPACE_ID, currentSession: owner, sessions: [owner] }),
      apiPort: API_PORT, gatewayUserAccess: testGatewayUserAccess(),
    });
    const responses: ReturnType<typeof createDeferredResult<Response>>[] = [];
    const errors: string[] = [];
    const timer = spyOn(globalThis, "setInterval");
    installGatewayFetch(({ path }) => {
      if (path === "/api/gateway/health") return apiSuccessResponse({ gateway_id: GATEWAY_ID });
      if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
        workspace_id: WORKSPACE_ID, catalog_revision: 7, event_seq_watermark: 0, generation: 1,
      });
      if (path === "/api/v1/session-catalog/navigation-events") return apiSuccessResponse({
        workspace_id: WORKSPACE_ID, event_seq_watermark: 0, items: [], next_cursor: null, has_more: false, cursor_gone: false,
      });
      if (path === `/api/v1/sessions/${owner.session_id}`) {
        const response = createDeferredResult<Response>();
        responses.push(response);
        return response.promise;
      }
      return undefined;
    });
    const runtime = createSessionCatalogOutboxRuntime({
      apiPort: API_PORT, reconcileIntervalMs: 60_000,
      openPersistence: async () => emptyPersistence, adapter: emptyAdapter,
      onError: (message) => errors.push(message),
      refreshSessionOwner: (target, signal) => refreshCurrentSessionCatalogOwner(
        API_PORT, target, mirror.current, mirror.setState, signal,
      ),
    });
    try {
      const access = testGatewayUserAccess();
      runtime.activatePrincipal(access);
      await runtime.openWorkspace(WORKSPACE_TARGET, access);
      expect(timer.mock.calls).toHaveLength(1);
      const handler = timer.mock.calls[0]![0];
      if (typeof handler !== "function") throw new Error("对账周期必须注册函数");
      const tick = () => handler();
      for (const attempt of [1, 2]) {
        tick();
        await flushReactEffects();
        expect(responses).toHaveLength(attempt);
        // 用同一在途 HTTP 跨越多个实际调度回调，不能把一次失败显示为多次。
        for (let index = 0; index < 3; index += 1) tick();
        expect(responses).toHaveLength(attempt);
        const response = apiErrorResponse(409, "session_deletion_pending");
        response.headers.set("X-Request-ID", `request-owner-pending-${attempt}`);
        responses[attempt - 1]!.release(response);
        await flushReactEffects();
        expect(errors).toEqual(Array.from({ length: attempt }, (_, index) =>
          `对账会话目录操作失败 (${GATEWAY_WORKSPACE_ID}): 请求失败 409 : session_deletion_pending [request_id=request-owner-pending-${index + 1}]`));
        expect(mirror.current().currentSession).toEqual(owner);
      }
      tick();
      await flushReactEffects();
      expect(responses).toHaveLength(3);
      const fresh = { ...owner, title: "新请求已恢复的完整 owner" };
      responses[2]!.release(apiSuccessResponse(fresh));
      await flushReactEffects();
      expect(mirror.current().currentSession).toEqual(fresh);
      expect(errors).toHaveLength(2);
    } finally {
      runtime.dispose();
      timer.mockRestore();
    }
  });

  for (const changedScope of ["会话", "工作区", "用户", "endpoint"] as const) {
    test(`owner GET 迟到404不能重置新${changedScope}scope`, async () => {
      const mirror = createAppStateMirror({
        ...createSessionHookState({ workspaceId: GATEWAY_WORKSPACE_ID, currentSession: owner, sessions: [owner, survivor] }),
        apiPort: API_PORT, gatewayUserAccess: testGatewayUserAccess(),
      });
      let started!: () => void;
      const requested = new Promise<void>((resolve) => { started = resolve; });
      let reply!: (response: Response) => void;
      const delayed = new Promise<Response>((resolve) => { reply = resolve; });
      let finished!: () => void;
      const refreshed = new Promise<void>((resolve) => { finished = resolve; });
      installGatewayFetch(({ path }) => {
        if (path === "/api/gateway/health") return apiSuccessResponse({ gateway_id: GATEWAY_ID });
        if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
          workspace_id: WORKSPACE_ID, catalog_revision: 7, event_seq_watermark: 0, generation: 1,
        });
        if (path === `/api/v1/sessions/${owner.session_id}`) { started(); return delayed; }
        return undefined;
      });
      const errors: string[] = [];
      const runtime = createSessionCatalogOutboxRuntime({
        apiPort: API_PORT, reconcileIntervalMs: 10,
        openPersistence: async () => emptyPersistence, adapter: emptyAdapter,
        onError: (message) => errors.push(message),
        async refreshSessionOwner(target, signal) {
          await refreshCurrentSessionCatalogOwner(API_PORT, target, mirror.current, mirror.setState, signal);
          finished();
        },
      });
      try {
        const access = testGatewayUserAccess();
        runtime.activatePrincipal(access);
        await runtime.openWorkspace(WORKSPACE_TARGET, access);
        await requested;
        mirror.setState((state) => changedScope === "会话" ? { ...state, currentSession: survivor }
          : changedScope === "工作区" ? { ...state, currentSessionWorkspaceId: "another-workspace" }
          : changedScope === "用户" ? { ...state, gatewayUserAccess: { ...access, user_id: "another-user" } }
          : { ...state, apiPort: API_PORT + 1 });
        const switched = mirror.current();
        reply(apiErrorResponse(404, "Session not found"));
        await refreshed;
        expect(mirror.current()).toBe(switched);
        expect(errors).toEqual([]);
      } finally { runtime.dispose(); }
    });
  }
});

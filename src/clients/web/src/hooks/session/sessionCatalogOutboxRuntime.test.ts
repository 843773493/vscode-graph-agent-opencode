import { afterEach, describe, expect, test } from "bun:test";
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
import { flushReactEffects } from "../../tests/fixtures/session/asyncControl";
import {
  mountSessionResourceExplorerHarness,
} from "../../tests/fixtures/session/sessionResourceExplorer";
import {
  catalogOutboxWorkspaceScopeKey,
  createSessionCatalogOutboxRuntime,
} from "./sessionCatalogOutboxRuntime";
import type { SessionCatalogOperationsAdapter } from "./sessionCatalogOutboxDriver";
import { refreshCurrentSessionCatalogOwner } from "./useSessionLifecycleActions";
import { sessionScopeKey } from "../../state/session/sessionScope";

const API_PORT = 49_441;
const GATEWAY_ID = "gateway-test";
const GATEWAY_WORKSPACE_ID = "gateway-route-test";
const BACKEND_WORKSPACE_ID = "c6dd923e-4a7c-4db3-8c63-110ea072d3a4";
const WORKSPACE_TARGET: GatewayWorkspaceRequestTarget = {
  workspace_id: GATEWAY_WORKSPACE_ID,
  backend_workspace_id: BACKEND_WORKSPACE_ID,
  connection_kind: "local",
};
const OPERATION_ID = "op_00000000000000000000000000000001";

function navigationEvent(sequence: number): SessionCatalogNavigationEvent {
  return { event_seq: sequence, workspace_id: BACKEND_WORKSPACE_ID,
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

function persistedOperation(): {
  record: CatalogOutboxStoredOperation;
} {
  const partition: CatalogOutboxPartition = {
    gatewayId: GATEWAY_ID,
    workspaceId: BACKEND_WORKSPACE_ID,
    principal: "guest",
  };
  const outbox = createCatalogOutbox(partition);
  const pending = addCatalogOutboxIntent(
    outbox,
    OPERATION_ID,
    { kind: "rename_node", targetNodeId: "cnode_1", name: "新名字" },
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
    if (path === "/api/v1/workspace") return apiSuccessResponse({ workspace_id: BACKEND_WORKSPACE_ID });
    if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
      workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: catalogRevision,
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
        return { workspace_id: BACKEND_WORKSPACE_ID, accepted_count: accepted.length,
          receipts: accepted, created_node_ids: {} };
      },
      queryStatus: async (_port, target, ids) => {
        expect(target).toEqual(WORKSPACE_TARGET);
        queryStarted();
        await repairGate;
        catalogRevision = 4;
        return { workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: catalogRevision,
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
    }
  });
  return { runtime, records, deleted, enqueued, errors, phases, folder, detail,
    queried, releaseRepair, refreshFailure, runtimeOptions };
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

      await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 0));
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
      if (path === "/api/v1/workspace") {
        return apiSuccessResponse({ workspace_id: BACKEND_WORKSPACE_ID });
      }
      if (path === "/api/v1/session-catalog/snapshot") {
        return apiSuccessResponse({ workspace_id: BACKEND_WORKSPACE_ID,
          catalog_revision: 7, event_seq_watermark: 0, generation: 1 });
      }
      if (path === "/api/v1/session-catalog/navigation-events") {
        return apiSuccessResponse({ workspace_id: BACKEND_WORKSPACE_ID, event_seq_watermark: 0,
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
      allowStatus({
        workspace_id: BACKEND_WORKSPACE_ID,
        catalog_revision: 7,
        items: [rejectedReceipt()],
        unknown_operation_ids: [],
      });
      // 等待 outbox 清理完成；删除回调记录当时已交付的错误。
      await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 0));

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
        if (path === "/api/v1/workspace") return apiSuccessResponse({ workspace_id: BACKEND_WORKSPACE_ID });
        if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
          workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: 12, event_seq_watermark: 0, generation: 1,
        });
        if (path === "/api/v1/session-catalog/navigation-events") return apiSuccessResponse({
          workspace_id: BACKEND_WORKSPACE_ID, event_seq_watermark: 0, items: [],
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
            return { workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: 12,
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
  for (const mode of ["乱序重复", "事件缺口", "cursor gone", "分页"] as const) {
    test(`${mode}只使 confirmed 失效并读取最新 snapshot，不制造目录命令`, async () => {
      const errors: string[] = [];
      const refreshed: number[] = [];
      const requestedAfter: number[] = [];
      let watermark = 0;
      let ready!: () => void;
      const initialized = new Promise<void>((resolve) => { ready = resolve; });
      let completed!: () => void;
      const reconciled = new Promise<void>((resolve) => { completed = resolve; });
      let afterRefresh!: () => void;
      const resumed = new Promise<void>((resolve) => { afterRefresh = resolve; });
      installGatewayFetch(({ path, url, init }) => {
        if (path === "/api/gateway/health") return apiSuccessResponse({ gateway_id: GATEWAY_ID });
        if (path === "/api/v1/workspace") return apiSuccessResponse({ workspace_id: BACKEND_WORKSPACE_ID });
        if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
          workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: 7 + watermark,
          event_seq_watermark: watermark, generation: 1,
        });
        if (path === "/api/v1/session-catalog/navigation-events") {
          expect(init?.signal?.aborted).toBe(false);
          const query = new URL(url).searchParams;
          const after = Number(query.get("after"));
          requestedAfter.push(after);
          if (after === 2) afterRefresh();
          const page: SessionCatalogNavigationEventsPage = {
            workspace_id: BACKEND_WORKSPACE_ID, event_seq_watermark: watermark,
            items: [], next_cursor: null, has_more: false, cursor_gone: false,
          };
          if (watermark === 2 && after < 2) {
            if (mode === "乱序重复") page.items = [navigationEvent(2), navigationEvent(1), navigationEvent(1)];
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
        expect(refreshed).toEqual([7, 9]);
        expect(requestedAfter).toContain(2);
        expect(handle.driver.current().operations).toEqual([]);
        expect(errors).toEqual([]);
      } finally { runtime.dispose(); }
    });
  }
});

describe("navigation 对账中的已打开会话 owner", () => {
  const owner: Session = {
    session_id: "ses_catalog_owner", workspace_id: BACKEND_WORKSPACE_ID,
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
      if (path === "/api/v1/workspace") return apiSuccessResponse({ workspace_id: BACKEND_WORKSPACE_ID });
      if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
        workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: revision, event_seq_watermark: 0, generation: 1,
      });
      if (path === "/api/v1/session-catalog/navigation-events") return apiSuccessResponse({
        workspace_id: BACKEND_WORKSPACE_ID, event_seq_watermark: 0, items: [], next_cursor: null, has_more: false, cursor_gone: false,
      });
      if (path === "/api/v1/sessions") { listCalls += 1; throw new Error("owner失效不能重拉全量列表"); }
      if (path === `/api/v1/sessions/${owner.session_id}`) {
        expect(init?.signal?.aborted).toBe(false);
        return status === 200 ? apiSuccessResponse(fresh)
          : apiErrorResponse(status, status === 409 ? "会话日期目录正在隔离" : "Session not found");
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
        if (path === "/api/v1/workspace") return apiSuccessResponse({ workspace_id: BACKEND_WORKSPACE_ID });
        if (path === "/api/v1/session-catalog/snapshot") return apiSuccessResponse({
          workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: 7, event_seq_watermark: 0, generation: 1,
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

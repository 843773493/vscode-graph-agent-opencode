import { afterEach, describe, expect, test } from "bun:test";
import type {
  SessionCatalogOperationReceipt,
  SessionCatalogOperationStatusPage,
} from "../../api/session/sessionCatalogOperations";
import {
  addCatalogOutboxIntent,
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
import type { GatewayUserAccess } from "../../types/backend";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import {
  apiResponse,
  installGatewayFetch,
  restoreSessionHookGlobals,
  testGatewayUserAccess,
} from "./sessionHookTestFixtures";
import {
  catalogOutboxWorkspaceScopeKey,
  createSessionCatalogOutboxRuntime,
} from "./sessionCatalogOutboxRuntime";
import type { SessionCatalogOperationsAdapter } from "./sessionCatalogOutboxDriver";

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

afterEach(restoreSessionHookGlobals);

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
    pending_settlement: false,
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
        return apiResponse({ gateway_id: GATEWAY_ID });
      }
      if (path === "/api/v1/workspace") {
        return apiResponse({ workspace_id: BACKEND_WORKSPACE_ID });
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
    test(`${failure}保留持久 pending 与真实错误，后注册 owner 可恢复`, async () => {
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
        if (path === "/api/gateway/health") return apiResponse({ gateway_id: GATEWAY_ID });
        if (path === "/api/v1/workspace") return apiResponse({ workspace_id: BACKEND_WORKSPACE_ID });
        return undefined;
      });
      const runtime = createSessionCatalogOutboxRuntime({ apiPort: API_PORT, reconcileIntervalMs: 60_000,
        onError: (message) => errors.push(message),
        openPersistence: async () => ({
          load: async () => records,
          insert: async () => { throw new Error("恢复用例不应新增命令"); },
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
        expect(records).toEqual([record]);
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

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

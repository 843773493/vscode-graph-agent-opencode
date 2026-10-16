import { describe, expect, test } from "bun:test";
import { HttpRequestError } from "../../api/http";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import type {
  SessionCatalogEnqueueResult,
  SessionCatalogOperationReceipt,
  SessionCatalogOperationStatusPage,
} from "../../api/session/sessionCatalogOperations";
import {
  addCatalogOutboxIntent,
  applyCatalogOutboxOperationReceipt,
  assignCatalogOutboxOperationSequence,
  catalogOutboxBatchToIntents,
  createCatalogOutbox,
  type CatalogOutbox,
  type CatalogOutboxOperation,
} from "../../state/session/sessionCatalogOutbox";
import {
  createCatalogOutboxBroadcaster,
  createSessionCatalogOutboxDriver,
  type CatalogOutboxDriver,
  type SessionCatalogOperationsAdapter,
} from "./sessionCatalogOutboxDriver";
import {
  type CatalogOutboxPersistencePort,
  type CatalogOutboxStoredOperation,
} from "../../state/session/sessionCatalogOutboxStore";

const BACKEND_WORKSPACE_ID = "ca3f2988-8632-4b4b-bcba-055e6f3d8a21";
const PARTITION = {
  gatewayId: "local:8014",
  workspaceId: BACKEND_WORKSPACE_ID,
  principal: "guest",
};
const PARTITION_KEY = `${PARTITION.gatewayId}\u0000${PARTITION.workspaceId}\u0000${PARTITION.principal}`;
const GATEWAY_WORKSPACE_ID = "gateway-route-42";
const WORKSPACE_TARGET: GatewayWorkspaceRequestTarget = {
  workspace_id: GATEWAY_WORKSPACE_ID,
  backend_workspace_id: BACKEND_WORKSPACE_ID,
  connection_kind: "local",
};
const PORT = 48_901;
type StoredCatalogOutboxOperation = CatalogOutboxStoredOperation["operation"];

function withStoredSequence(
  operation: CatalogOutboxOperation,
  sequence: number,
): StoredCatalogOutboxOperation {
  return { ...operation, client_sequence: sequence };
}

function opId(suffix: string): string {
  return `op_${suffix.padStart(32, "0")}`;
}

function receipt(
  operationId: string,
  overrides: Partial<SessionCatalogOperationReceipt> = {},
): SessionCatalogOperationReceipt {
  return {
    operation_id: operationId,
    client_sequence: 1,
    queue_seq: 1,
    kind: "rename_node",
    state: "queued",
    created_node_id: null,
    committed_catalog_revision: null,
    error_code: null,
    error_detail: null,
    settlement: null,
    receipt_revision: 1,
    updated_at: "2026-09-24T00:00:00Z",
    ...overrides,
  };
}

function deferred<T = void>(): {
  promise: Promise<T>;
  resolve: (value: T | PromiseLike<T>) => void;
} {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}

function mergeStoredReceipts(
  records: readonly CatalogOutboxStoredOperation[],
  receipts: readonly SessionCatalogOperationReceipt[],
): CatalogOutboxStoredOperation[] {
  const byId = new Map(records.map((record) => [record.operation.client_operation_id, record]));
  const updated = new Map<string, CatalogOutboxStoredOperation>();
  for (const item of receipts) {
    const existing = updated.get(item.operation_id) ?? byId.get(item.operation_id);
    if (!existing) continue;
    const operation = applyCatalogOutboxOperationReceipt(existing.operation, item);
    updated.set(item.operation_id, {
      ...existing, operation: { ...operation, client_sequence: existing.client_sequence },
    });
  }
  return [...updated.values()];
}

/** 记录端口写入的确定性内存桩：用于验证「持久化先于入队」与时序，不模拟 IndexedDB。 */
function recordingPort(): CatalogOutboxPersistencePort & { writes: CatalogOutboxOperation[] } {
  const writes: CatalogOutboxOperation[] = [];
  const records = new Map<string, Map<number, CatalogOutboxStoredOperation>>();
  const nextSequences = new Map<string, number>();
  return {
    writes,
    async load(partitionKey) {
      return [...(records.get(partitionKey)?.values() ?? [])];
    },
    async insert(partitionKey, operation) {
      if (operation.client_sequence !== null) {
        throw new Error("driver 必须把未赋号的 pending 提交给持久层");
      }
      const bucket = records.get(partitionKey) ?? new Map();
      if ([...bucket.values()].some(
        (record) => record.operation.client_operation_id === operation.client_operation_id,
      )) {
        throw new Error("duplicate operation ID");
      }
      const sequence = nextSequences.get(partitionKey) ?? 1;
      const stored: CatalogOutboxStoredOperation = {
        partition_key: partitionKey,
        client_sequence: sequence,
        operation: { ...operation, client_sequence: sequence },
      };
      bucket.set(sequence, stored);
      records.set(partitionKey, bucket);
      nextSequences.set(partitionKey, sequence + 1);
      writes.push(stored.operation);
      return stored;
    },
    async applyReceipts(partitionKey, receipts) {
      const bucket = records.get(partitionKey);
      const updated = mergeStoredReceipts([...(bucket?.values() ?? [])], receipts);
      for (const record of updated) bucket?.set(record.client_sequence, record);
      return updated;
    },
    async delete(partitionKey, clientOperationIds) {
      const bucket = records.get(partitionKey);
      if (!bucket) return;
      for (const [sequence, record] of bucket) {
        if (clientOperationIds.includes(record.operation.client_operation_id)) bucket.delete(sequence);
      }
    },
  };
}

function renameIntent(): Parameters<CatalogOutboxDriver["applyIntent"]>[1] {
  return { kind: "rename_node", targetNodeId: "cnode_1", name: "新名字" };
}

describe("会话目录 outbox 驱动：持久化先于入队", () => {
  test("空回执发布不会覆盖持久化快照后加入的 pending", async () => {
    const persistence = recordingPort();
    const enqueued: string[][] = [];
    const operationId = opId("pending-during-empty-receipt");
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter: {
        async enqueue(_port, _target, intents) {
          const operationIds = intents.map((intent) => intent.client_operation_id);
          enqueued.push(operationIds);
          return { workspace_id: BACKEND_WORKSPACE_ID, accepted_count: intents.length,
            receipts: operationIds.map((id) => receipt(id)), created_node_ids: {} };
        },
        async queryStatus() { throw new Error("空 outbox 不应查询状态"); },
      },
    });
    await driver.restore();

    const reconciliation = driver.reconcile();
    // 让空回执完成持久化并生成 reported 快照，再在 publish 前加入新命令。
    await Promise.resolve();
    await Promise.resolve();
    const { dispatch } = await driver.applyIntent(operationId, renameIntent(), {
      baseCatalogRevision: 7, expectedRevision: 3,
    });

    expect(await dispatch).toBe("accepted");
    await reconciliation;
    expect(enqueued).toEqual([[operationId]]);
    expect(persistence.writes.map((operation) => operation.client_operation_id)).toEqual([operationId]);
    expect((await persistence.load(PARTITION_KEY)).map((record) => record.operation.client_operation_id))
      .toEqual([operationId]);
    expect(driver.current().operations).toMatchObject([{
      client_operation_id: operationId,
      state: "accepted",
      receipt_revision: 1,
    }]);
  });

  test("非空回执发布保留快照后加入的 pending 与更新后的 receipt revision", async () => {
    const persistence = recordingPort();
    const enqueued: string[][] = [];
    const firstOperationId = opId("tracked-before-reconcile");
    const pendingOperationId = opId("pending-during-receipt");
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter: {
        async enqueue(_port, _target, intents) {
          const operationIds = intents.map((intent) => intent.client_operation_id);
          enqueued.push(operationIds);
          return { workspace_id: BACKEND_WORKSPACE_ID, accepted_count: intents.length,
            receipts: operationIds.map((id) => receipt(id)), created_node_ids: {} };
        },
        async queryStatus(_port, _target, operationIds) {
          expect(operationIds).toEqual([firstOperationId]);
          return { workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: 8,
            items: [receipt(firstOperationId, { state: "running", receipt_revision: 2 })],
            unknown_operation_ids: [] };
        },
      },
    });
    await driver.restore();
    const first = await driver.applyIntent(firstOperationId, renameIntent(), {
      baseCatalogRevision: 7, expectedRevision: 3,
    });
    expect(await first.dispatch).toBe("accepted");

    const reconciliation = driver.reconcile();
    // queryStatus 与回执持久化均自然 resolve；在 reported 已生成、publish 尚未运行的微任务间隙插入 B。
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    const concurrent = await driver.applyIntent(pendingOperationId, {
      kind: "rename_node", targetNodeId: "cnode_2", name: "并发新名字",
    }, { baseCatalogRevision: 8, expectedRevision: 1 });

    expect(await concurrent.dispatch).toBe("accepted");
    await reconciliation;
    expect(enqueued).toEqual([[firstOperationId], [pendingOperationId]]);
    expect(persistence.writes.map((operation) => operation.client_operation_id))
      .toEqual([firstOperationId, pendingOperationId]);
    expect((await persistence.load(PARTITION_KEY)).map((record) => record.operation.client_operation_id))
      .toEqual([firstOperationId, pendingOperationId]);
    expect(driver.current().operations).toMatchObject([
      { client_operation_id: firstOperationId, state: "accepted", server_state: "running", receipt_revision: 2 },
      { client_operation_id: pendingOperationId, state: "accepted", receipt_revision: 1 },
    ]);
  });

  test("本地提交返回时 enqueue HTTP 仍阻塞，dispatch 独立取得真实回执", async () => {
    const request = deferred<SessionCatalogEnqueueResult>();
    const started = deferred<void>();
    const persistence = recordingPort();
    const driver = createSessionCatalogOutboxDriver({
      port: PORT, gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET, partition: PARTITION, persistence,
      adapter: {
        async enqueue() { started.resolve(); return await request.promise; },
        async queryStatus() { throw new Error("本用例不查询状态"); },
      },
    });
    await driver.restore();
    const { dispatch } = await driver.applyIntent(opId("local"), renameIntent(), {
      baseCatalogRevision: 7, expectedRevision: 3,
    });
    await started.promise;
    let dispatched = false;
    void dispatch.then(() => { dispatched = true; });
    expect(dispatched).toBe(false);
    expect(driver.current().operations[0].state).toBe("persisted");
    expect((await persistence.load(PARTITION_KEY))[0].operation.client_operation_id).toBe(opId("local"));
    request.resolve({ workspace_id: BACKEND_WORKSPACE_ID, accepted_count: 1,
      receipts: [receipt(opId("local"))], created_node_ids: {} });
    expect(await dispatch).toBe("accepted");
    expect(driver.current().operations[0].state).toBe("accepted");
  });

  test("同步 pending 立即可见，随后落盘并批量入队", async () => {
    const events: string[] = [];
    const enqueued: string[][] = [];
    const enqueuedTargets: GatewayWorkspaceRequestTarget[] = [];
    const queriedTargets: GatewayWorkspaceRequestTarget[] = [];
    const persistence = recordingPort();
    const adapter: SessionCatalogOperationsAdapter = {
      async enqueue(_port, workspaceTarget, intents) {
        enqueuedTargets.push(workspaceTarget);
        events.push("enqueue");
        enqueued.push(intents.map((intent) => intent.client_operation_id));
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          accepted_count: intents.length,
          receipts: intents.map((intent) => receipt(intent.client_operation_id)),
          created_node_ids: {},
        } satisfies SessionCatalogEnqueueResult;
      },
      async queryStatus(_port, workspaceTarget, operationIds) {
        queriedTargets.push(workspaceTarget);
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          catalog_revision: 7,
          items: operationIds.map((operationId) => receipt(operationId)),
          unknown_operation_ids: [],
        };
      },
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter,
      onChange: (outbox) => {
        const last = outbox.operations[outbox.operations.length - 1];
        events.push(`change:${last?.state ?? "empty"}`);
      },
    });
    await driver.restore();

    const outcome = await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);

    expect(outcome).toBe("accepted");
    // 状态时序：restore 空 outbox → pending_local 同步出现 → persisted → accepted。
    expect(events).toEqual([
      "change:empty",
      "change:pending_local",
      "change:persisted",
      "enqueue",
      "change:accepted",
    ]);
    expect(persistence.writes.map((operation) => operation.state)).toEqual(["pending_local"]);
    expect(enqueued).toEqual([[opId("a")]]);
    expect(enqueuedTargets).toEqual([WORKSPACE_TARGET]);
    expect((await persistence.load(PARTITION_KEY))
      .map((record) => record.operation.client_operation_id)).toEqual([opId("a")]);
    expect(await persistence.load(
      `${PARTITION.gatewayId}\u0000${GATEWAY_WORKSPACE_ID}\u0000${PARTITION.principal}`,
    )).toEqual([]);
    expect(driver.current().operations[0].state).toBe("accepted");

    await driver.reconcile();
    expect(queriedTargets).toEqual([WORKSPACE_TARGET]);
  });

  test("本地持久化失败时撤销 pending、显示错误且绝不派发入队", async () => {
    let enqueueCount = 0;
    const persistence: CatalogOutboxPersistencePort = {
      load: async () => [],
      insert: async () => {
        throw new Error("QuotaExceededError: 本地存储已满");
      },
      applyReceipts: async () => { throw new Error("持久化失败时不得合并回执"); },
      delete: async () => undefined,
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter: {
        async enqueue() {
          enqueueCount += 1;
          throw new Error("持久化失败时不得派发入队");
        },
        async queryStatus() {
          throw new Error("本用例不应查询状态");
        },
      },
    });
    await driver.restore();

    await expect(driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch)).rejects.toThrow("会话目录 pending 变更未能本地持久化，已撤销该操作及依赖");

    expect(enqueueCount).toBe(0);
    expect(driver.current().operations).toEqual([]);
  });

  test("唯一 insert 事务失败时撤销 pending、保持 IDB 无记录且绝不派发", async () => {
    const disk = new Map<number, StoredCatalogOutboxOperation>();
    let failNextInsert = false;
    const persistence: CatalogOutboxPersistencePort = {
      load: async () => [...disk.entries()]
        .sort(([left], [right]) => left - right)
        .map(([clientSequence, operation]) => ({
          partition_key: PARTITION_KEY,
          client_sequence: clientSequence,
          operation,
        })),
      insert: async (partitionKey, operation) => {
        if (failNextInsert) {
          failNextInsert = false;
          throw new Error("QuotaExceededError: 本地存储已满");
        }
        if (operation.client_sequence !== null) throw new Error("pending 已提前赋号");
        const sequence = disk.size + 1;
        const stored = {
          partition_key: partitionKey,
          client_sequence: sequence,
          operation: withStoredSequence(operation, sequence),
        };
        disk.set(sequence, stored.operation);
        return stored;
      },
      applyReceipts: async (partitionKey, receipts) => {
        const updated = mergeStoredReceipts([...disk].map(([clientSequence, operation]) => ({
          partition_key: partitionKey, client_sequence: clientSequence, operation,
        })), receipts);
        for (const record of updated) disk.set(record.client_sequence, record.operation);
        return updated;
      },
      delete: async (_partitionKey, clientOperationIds) => {
        for (const [clientSequence, operation] of disk) {
          if (clientOperationIds.includes(operation.client_operation_id)) disk.delete(clientSequence);
        }
      },
    };
    const enqueued: string[] = [];
    const adapter: SessionCatalogOperationsAdapter = {
      async enqueue(_port, _workspaceId, intents) {
        for (const intent of intents) enqueued.push(intent.client_operation_id);
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          accepted_count: intents.length,
          receipts: intents.map((intent) => receipt(intent.client_operation_id)),
          created_node_ids: {},
        };
      },
      async queryStatus() {
        throw new Error("本用例不应查询状态");
      },
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter,
    });
    await driver.restore();
    await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);

    failNextInsert = true;
    await expect(driver.applyIntent(opId("b"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch)).rejects.toThrow("已撤销该操作及依赖");

    // 磁盘上不得留下失败命令，刷新恢复后也不能被重新派发。
    expect([...disk.values()].map((operation) => operation.client_operation_id))
      .not.toContain(opId("b"));
    const resumed = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter,
    });
    const restored = await resumed.restore();
    expect(restored.operations.map((operation) => operation.client_operation_id))
      .not.toContain(opId("b"));
    const before = enqueued.length;
    await resumed.applyIntent(opId("c"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);
    expect(enqueued.slice(before)).not.toContain(opId("b"));
  });

  test("本地 insert 失败且回滚也失败时，错误必须同时含写入与回滚两条原因", async () => {
    const persistence: CatalogOutboxPersistencePort = {
      load: async () => [],
      insert: async () => {
        // IndexedDB 事务可能在本地提交后才报告失败，必须按稳定 ID 清理残留。
        throw new Error("QuotaExceededError: 本地存储已满");
      },
      applyReceipts: async () => { throw new Error("持久化失败时不得合并回执"); },
      delete: async () => {
        throw new Error("删除已落盘条目也失败");
      },
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter: {
        async enqueue() { throw new Error("持久化失败时不得派发入队"); },
        async queryStatus() { throw new Error("本用例不应查询状态"); },
      },
    });
    await driver.restore();

    const failure = await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch).then(() => null, (error: Error) => error);

    expect(failure?.message).toContain("回滚也失败");
    expect(failure?.message).toContain("QuotaExceededError");
    expect(failure?.message).toContain("删除已落盘条目也失败");
  });

  test("刷新恢复 outbox 保留已落盘的 acceptance 与原命令身份", async () => {
    const persistence = recordingPort();
    const enqueued: string[][] = [];
    const adapter: SessionCatalogOperationsAdapter = {
      async enqueue(_port, _workspaceId, intents) {
        enqueued.push(intents.map((intent) => intent.client_operation_id));
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          accepted_count: intents.length,
          receipts: intents.map((intent) => receipt(intent.client_operation_id)),
          created_node_ids: {},
        };
      },
      async queryStatus() {
        throw new Error("本用例不应查询状态");
      },
    };
    const first = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter,
    });
    await first.restore();
    await first.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);

    // 模拟刷新：新 driver 从同一持久层恢复，序号与 operation ID 都来自原记录。
    expect(persistence.writes.map((operation) => operation.client_operation_id)).toEqual([opId("a")]);
    expect(enqueued).toEqual([[opId("a")]]);
    const resumed = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter,
    });
    const restored = await resumed.restore();
    expect(restored.operations.map((operation) => operation.client_operation_id)).toEqual([opId("a")]);
    expect(restored.operations[0].state).toBe("accepted");
    expect(restored.operations[0].receipt_revision).toBe(1);
  });

  test("回执写入失败不发布 acceptance，按原 ID 对账后才推进镜像", async () => {
    const base = recordingPort();
    const writing = deferred<void>();
    const release = deferred<void>();
    let fail = true;
    const states: string[] = [];
    const persistence: CatalogOutboxPersistencePort = {
      ...base,
      async applyReceipts(key, receipts) {
        if (fail) {
          writing.resolve();
          await release.promise;
          throw new Error("AbortError: 回执事务失败");
        }
        return await base.applyReceipts(key, receipts);
      },
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT, gatewayWorkspaceId: GATEWAY_WORKSPACE_ID, workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION, persistence,
      onChange(outbox) { states.push(...outbox.operations.map((operation) => operation.state)); },
      adapter: {
        async enqueue(_port, _target, intents) {
          return { workspace_id: BACKEND_WORKSPACE_ID, accepted_count: intents.length,
            receipts: intents.map((intent) => receipt(intent.client_operation_id)), created_node_ids: {} };
        },
        async queryStatus(_port, _target, ids) {
          expect(ids).toEqual([opId("durable")]);
          return { workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: 7,
            items: [receipt(opId("durable"), { state: "running", receipt_revision: 2 })],
            unknown_operation_ids: [] };
        },
      },
    });
    await driver.restore();
    const { dispatch } = await driver.applyIntent(opId("durable"), renameIntent(), {
      baseCatalogRevision: 7, expectedRevision: 3,
    });
    const dispatched = dispatch.then(() => null, (error: Error) => error);
    await writing.promise;
    expect(states).not.toContain("accepted");
    expect(driver.current().operations[0].state).toBe("persisted");
    release.resolve();
    expect((await dispatched)?.message).toContain("回执事务失败");
    expect(states).not.toContain("accepted");
    expect((await base.load(PARTITION_KEY))[0].operation.receipt_revision).toBeNull();
    fail = false;
    await driver.reconcile();
    expect(driver.current().operations[0]).toMatchObject({
      client_operation_id: opId("durable"), client_sequence: 1, state: "accepted", receipt_revision: 2,
    });
    expect(driver.current().operations[0]).toEqual((await base.load(PARTITION_KEY))[0].operation);
    expect(base.writes).toHaveLength(1);
  });

  test("回执提交等待期间的成功 reload 与剪枝不被派发误报为 rejected", async () => {
    const base = recordingPort();
    const writing = deferred<void>();
    const release = deferred<void>();
    const persistence: CatalogOutboxPersistencePort = {
      ...base,
      async applyReceipts(key, receipts) {
        const stored = await base.applyReceipts(key, receipts);
        writing.resolve();
        await release.promise;
        return stored;
      },
    };
    const operationId = opId("settled");
    const driver = createSessionCatalogOutboxDriver({
      port: PORT, gatewayWorkspaceId: GATEWAY_WORKSPACE_ID, workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION, persistence,
      adapter: {
        async enqueue() {
          return { workspace_id: BACKEND_WORKSPACE_ID, accepted_count: 1,
            receipts: [receipt(operationId, { kind: "delete_session", state: "committed",
              settlement: "settled", receipt_revision: 2, committed_catalog_revision: 12 })],
            created_node_ids: {} };
        },
        async queryStatus() { throw new Error("已取得 settled 回执不需重查"); },
      },
    });
    await driver.restore();
    const { dispatch } = await driver.applyIntent(operationId, {
      kind: "delete_session", targetNodeId: "session_deleted",
    }, { baseCatalogRevision: 7 });
    await writing.promise;
    await driver.reload();
    expect(driver.current().operations[0].settlement).toBe("settled");
    await driver.prune(12);
    expect(driver.current().operations).toEqual([]);
    release.resolve();
    expect(await dispatch).toBe("accepted");
    expect(driver.current().operations).toEqual([]);
    expect(await base.load(PARTITION_KEY)).toEqual([]);
    expect(base.writes).toHaveLength(1);
  });

  test("跨 tab 的迟到回执和 reload 都消费较新持久版本，repair 冷恢复后不剪枝", async () => {
    const persistence = recordingPort();
    const operationId = opId("repair");
    const pending = addCatalogOutboxIntent(createCatalogOutbox(PARTITION), operationId, {
      kind: "delete_session", targetNodeId: "session_deleted",
    }, { baseCatalogRevision: 7 }).operations[0];
    await persistence.insert(PARTITION_KEY, pending);
    const delayedStatus = deferred<SessionCatalogOperationStatusPage>();
    const querying = deferred<void>();
    let delayed = true;
    let latest = receipt(operationId, { kind: "delete_session", state: "committed",
      committed_catalog_revision: 12, settlement: "repair_required", receipt_revision: 3,
      error_code: "cleanup_failed", error_detail: "排空需要修复" });
    const page = (item: SessionCatalogOperationReceipt): SessionCatalogOperationStatusPage => ({
      workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: 12, items: [item], unknown_operation_ids: [],
    });
    const makeDriver = (queryStatus: SessionCatalogOperationsAdapter["queryStatus"]) =>
      createSessionCatalogOutboxDriver({
        port: PORT, gatewayWorkspaceId: GATEWAY_WORKSPACE_ID, workspaceTarget: WORKSPACE_TARGET,
        partition: PARTITION, persistence,
        adapter: { async enqueue() { throw new Error("已有后端回执不得重新入队"); }, queryStatus },
      });
    const writer = makeDriver(async () => page(latest));
    const stale = makeDriver(async () => {
      if (delayed) { delayed = false; querying.resolve(); return await delayedStatus.promise; }
      return page(latest);
    });
    const reloader = makeDriver(async () => { throw new Error("磁盘仍有回执时 reload 不需重查"); });
    await Promise.all([writer.restore(), stale.restore(), reloader.restore()]);
    const reconciling = stale.reconcile();
    await querying.promise;
    await writer.reconcile();
    delayedStatus.resolve(page(receipt(operationId, { kind: "delete_session", receipt_revision: 1 })));
    await reconciling;
    await reloader.reload();
    const expected = writer.current().operations[0];
    expect(stale.current().operations[0]).toEqual(expected);
    expect(reloader.current().operations[0]).toEqual(expected);
    expect((await makeDriver(async () => page(latest)).restore()).operations[0]).toEqual(expected);
    expect((await stale.prune(12)).operations[0]).toEqual(expected);
    expect(expected).toEqual((await persistence.load(PARTITION_KEY))[0].operation);
    expect(expected).toMatchObject({ client_operation_id: operationId, client_sequence: 1,
      state: "committed", receipt_revision: 3, settlement: "repair_required" });

    latest = { ...latest, receipt_revision: 4, settlement: "settled", error_code: null, error_detail: null };
    await writer.reconcile();
    await writer.prune(12);
    expect(await persistence.applyReceipts(PARTITION_KEY, [receipt(operationId, {
      ...latest, receipt_revision: 3, settlement: "repair_required",
    })])).toEqual([]);
    expect(await persistence.load(PARTITION_KEY)).toEqual([]);
    // repair 镜像的磁盘行已由另一 tab 完成清理，仍需取得 settled 再结束本 tab 的等待。
    await stale.reload();
    expect(stale.current().operations[0].settlement).toBe("settled");
    expect((await stale.prune(12)).operations).toEqual([]);
    expect(persistence.writes).toHaveLength(1);
  });

  test("reload 合并外部持久化命令与读取期间本地新命令", async () => {
    const base = recordingPort();
    const loadStarted = deferred<void>();
    const releaseLoad = deferred<void>();
    let delayNextLoad = false;
    const persistence: CatalogOutboxPersistencePort = {
      ...base,
      async load(partitionKey) {
        const records = await base.load(partitionKey);
        if (delayNextLoad) {
          delayNextLoad = false;
          loadStarted.resolve();
          await releaseLoad.promise;
        }
        return records;
      },
    };
    const adapter: SessionCatalogOperationsAdapter = {
      async enqueue(_port, _workspaceId, intents) {
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          accepted_count: intents.length,
          receipts: intents.map((intent) => receipt(intent.client_operation_id)),
          created_node_ids: {},
        };
      },
      async queryStatus() { throw new Error("本用例不应查询状态"); },
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter,
    });
    await driver.restore();

    const otherTab = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter,
    });
    await otherTab.restore();
    await otherTab.applyIntent(opId("b"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);

    delayNextLoad = true;
    const reloading = driver.reload();
    await loadStarted.promise;
    await driver.applyIntent(opId("c"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);
    releaseLoad.resolve();
    await reloading;

    expect(driver.current().operations.map((operation) => operation.client_operation_id))
      .toEqual([opId("b"), opId("c")]);
    expect((await base.load(PARTITION_KEY))
      .map((record) => record.operation.client_operation_id))
      .toEqual([opId("b"), opId("c")]);
  });

  test("未 restore 即操作必须响亮失败", async () => {
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence: recordingPort(),
      adapter: {
        async enqueue() { throw new Error("不应入队"); },
        async queryStatus() { throw new Error("不应查询"); },
      },
    });
    expect(() => driver.current()).toThrow("会话目录 outbox 尚未恢复");
  });

  test("F1：落盘后进程在标记前退出，恢复的 pending_local 必须重放而非静默丢失", async () => {
    // 直接构造磁盘残留：driver.applyIntent 先按 pending_local 落盘、之后才在内存标记
    // persisted；模拟程序在该窗口退出，磁盘上留下的就是 pending_local。
    const seed = addCatalogOutboxIntent(
      createCatalogOutbox(PARTITION),
      opId("a"),
      { kind: "rename_node", targetNodeId: "cnode_1", name: "新名字" },
      { baseCatalogRevision: 7, expectedRevision: 3 },
    );
    const seededOperation = assignCatalogOutboxOperationSequence(
      seed,
      opId("a"),
      1,
    ).operations[0];
    const storedSeed = withStoredSequence(seededOperation, 1);
    const disk = new Map<number, StoredCatalogOutboxOperation>([[1, storedSeed]]);
    const persistence: CatalogOutboxPersistencePort = {
      async load() {
        return [...disk.entries()]
          .sort(([left], [right]) => left - right)
          .map(([clientSequence, operation]) => ({
          partition_key: PARTITION_KEY,
            client_sequence: clientSequence,
            operation,
          }));
      },
      async insert(partitionKey, operation) {
        if (operation.client_sequence !== null) throw new Error("pending 已提前赋号");
        const clientSequence = Math.max(0, ...disk.keys()) + 1;
        const stored = {
          partition_key: partitionKey,
          client_sequence: clientSequence,
          operation: withStoredSequence(operation, clientSequence),
        };
        disk.set(clientSequence, stored.operation);
        return stored;
      },
      async applyReceipts(partitionKey, receipts) {
        const updated = mergeStoredReceipts([...disk].map(([clientSequence, operation]) => ({
          partition_key: partitionKey, client_sequence: clientSequence, operation,
        })), receipts);
        for (const record of updated) disk.set(record.client_sequence, record.operation);
        return updated;
      },
      async delete(_partitionKey, clientOperationIds) {
        for (const [clientSequence, operation] of disk) {
          if (clientOperationIds.includes(operation.client_operation_id)) disk.delete(clientSequence);
        }
      },
    };
    const enqueued: string[][] = [];
    const adapter: SessionCatalogOperationsAdapter = {
      async enqueue(_port, _workspaceId, intents) {
        enqueued.push(intents.map((intent) => intent.client_operation_id));
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          accepted_count: intents.length,
          receipts: intents.map((intent) => receipt(intent.client_operation_id)),
          created_node_ids: {},
        } satisfies SessionCatalogEnqueueResult;
      },
      // F1 修复前该命令停留在 `pending_local`，不会被 `reconcile` 收进对账集合；
      // 归一为 persisted 后必须真的按 ID 查询：后端「不认识该 ID」即允许按同一 ID 重放。
      async queryStatus(_port, _workspaceId, operationIds) {
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          catalog_revision: 7,
          items: [],
          unknown_operation_ids: [...operationIds],
        } satisfies SessionCatalogOperationStatusPage;
      },
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter,
    });
    const restored = await driver.restore();
    // 归一为可重放态：既不留在 pending_local，也不落在对账集合之外。
    expect(restored.operations[0].state).toBe("persisted");

    // 对账时命中该 ID 并确认后端不认识它，随后 flush 把该命令按同一 ID 重新入队。
    await driver.reconcile();
    expect(enqueued).toEqual([[opId("a")]]);
    expect(driver.current().operations[0].state).toBe("accepted");
  });
});

describe("会话目录 outbox 驱动：未知结果与对账", () => {
  function driverWithAdapter(
    adapter: SessionCatalogOperationsAdapter,
    reconcileFailure?: Parameters<typeof createSessionCatalogOutboxDriver>[0]["reconcileFailure"],
  ): CatalogOutboxDriver {
    return createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence: recordingPort(),
      adapter,
      reconcileFailure,
    });
  }

  test("HTTP 超时归为 unknown：保留原 ID 不换 ID 重发", async () => {
    const driver = driverWithAdapter({
      async enqueue() {
        throw new Error("请求超时: /api/v1/session-catalog/operations:enqueue");
      },
      async queryStatus() {
        throw new Error("本用例不应查询状态");
      },
    });
    await driver.restore();
    const outcome = await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);
    expect(outcome).toBe("unknown");
    expect(driver.current().operations.map((operation) => operation.state)).toEqual(["unknown"]);
    expect(driver.current().operations[0].client_operation_id).toBe(opId("a"));
  });

  test("503 背压归为 unknown 并保留 outbox", async () => {
    const driver = driverWithAdapter({
      async enqueue() {
        throw new HttpRequestError(503, "Service Unavailable", "backpressure", "/enqueue");
      },
      async queryStatus() {
        throw new Error("本用例不应查询状态");
      },
    });
    await driver.restore();
    const outcome = await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);
    expect(outcome).toBe("unknown");
    expect(driver.current().operations.length).toBe(1);
    expect(driver.current().operations[0].state).toBe("unknown");
  });

  test("4xx 明确拒绝：重读权威状态、移除失败项并报 rejected", async () => {
    const driver = driverWithAdapter({
      async enqueue() {
        throw new HttpRequestError(409, "Conflict", "revision conflict", "/enqueue");
      },
      async queryStatus(_port, _workspaceId, operationIds): Promise<SessionCatalogOperationStatusPage> {
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          catalog_revision: 12,
          items: [receipt(operationIds[0], {
            state: "rejected",
            error_code: "revision_changed",
            error_detail: "目标 node 已被其它客户端修改",
          })],
          unknown_operation_ids: [],
        };
      },
    }, async (reported, revision, finalize) => {
      expect(revision).toBe(12);
      expect(reported.operations[0].state).toBe("rejected");
      await finalize();
    });
    await driver.restore();
    const outcome = await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);
    expect(outcome).toBe("rejected");
    expect(driver.current().operations).toEqual([]);
  });

  test("拒绝对账与未完成 insert 交错时，依赖闭包和独立命令在磁盘与内存一致", async () => {
    const base = recordingPort();
    const bInserted = deferred<void>();
    const releaseBInsert = deferred<void>();
    const deleteStarted = deferred<void>();
    const releaseDelete = deferred<void>();
    const enqueueStarted = deferred<void>();
    const rejectA = deferred<void>();
    const cPersisted = deferred<void>();
    const deleteCalls: string[][] = [];
    const enqueueCalls: string[][] = [];
    const persistence: CatalogOutboxPersistencePort = {
      ...base,
      async insert(partitionKey, operation) {
        const stored = await base.insert(partitionKey, operation);
        if (operation.client_operation_id === opId("b")) {
          bInserted.resolve();
          await releaseBInsert.promise;
        }
        return stored;
      },
      async delete(partitionKey, clientOperationIds) {
        deleteCalls.push([...clientOperationIds]);
        if (deleteCalls.length === 1) deleteStarted.resolve();
        await releaseDelete.promise;
        await base.delete(partitionKey, clientOperationIds);
      },
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      async reconcileFailure(_reported, revision, finalize) {
        expect(revision).toBe(12);
        await finalize();
      },
      adapter: {
        async enqueue(_port, _workspaceId, intents) {
          const operationIds = intents.map((intent) => intent.client_operation_id);
          enqueueCalls.push(operationIds);
          if (enqueueCalls.length === 1) {
            enqueueStarted.resolve();
            await rejectA.promise;
            throw new HttpRequestError(409, "Conflict", "revision conflict", "/enqueue");
          }
          return {
            workspace_id: BACKEND_WORKSPACE_ID,
            accepted_count: intents.length,
            receipts: intents.map((intent) => receipt(intent.client_operation_id)),
            created_node_ids: {},
          };
        },
        async queryStatus(_port, _workspaceId, operationIds) {
          return {
            workspace_id: BACKEND_WORKSPACE_ID,
            catalog_revision: 12,
            items: [receipt(operationIds[0], {
              kind: "create_folder",
              state: "rejected",
              error_code: "revision_changed",
              error_detail: "目录版本已变化",
            })],
            unknown_operation_ids: [],
          };
        },
      },
      onChange(outbox) {
        for (const operation of outbox.operations) {
          if (operation.state !== "persisted") continue;
          if (operation.client_operation_id === opId("c")) cPersisted.resolve();
        }
      },
    });
    await driver.restore();

    const first = driver.applyIntent(opId("a"), {
      kind: "create_folder",
      name: "待创建目录",
    }, { baseCatalogRevision: 7 }).then(({ dispatch }) => dispatch);
    await enqueueStarted.promise;
    const dependent = driver.applyIntent(opId("b"), {
      kind: "move_node",
      targetNodeId: "cnode_2",
      parentCreatedByOperationId: opId("a"),
    }, { baseCatalogRevision: 7, expectedRevision: 2, dependsOn: [opId("a")] }).then(({ dispatch }) => dispatch);
    await bInserted.promise;
    const independent = driver.applyIntent(opId("c"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);
    await cPersisted.promise;

    rejectA.resolve();
    await deleteStarted.promise;
    // B 的 IDB 行已提交，但它的 insert continuation 尚未把序号回填到内存。
    releaseBInsert.resolve();
    releaseDelete.resolve();
    const outcomes = await Promise.allSettled([first, dependent, independent]);
    expect(outcomes[0]).toMatchObject({ status: "fulfilled", value: "rejected" });
    expect(outcomes[1]).toMatchObject({ status: "fulfilled", value: "rejected" });
    expect(outcomes[2]).toMatchObject({ status: "fulfilled", value: "accepted" });
    expect(deleteCalls[0]).toEqual([opId("a"), opId("b")]);
    expect(enqueueCalls).toEqual([[opId("a")], [opId("c")]]);

    expect(driver.current().operations.map((operation) => operation.client_operation_id))
      .toEqual([opId("c")]);
    expect(driver.current().operations[0].state).toBe("accepted");
    const restored = await createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter: {
        async enqueue() { throw new Error("本用例不应重放 outbox"); },
        async queryStatus() { throw new Error("本用例不应查询状态"); },
      },
    }).restore();
    expect(restored.operations.map((operation) => operation.client_operation_id))
      .toEqual([opId("c")]);
    expect(restored.operations[0].state).toBe("accepted");
    expect(restored.operations[0].receipt_revision).toBe(1);
  });

  test("对账时服务端不认识该 ID 才允许按同一 ID 重试", async () => {
    const attempts: string[][] = [];
    let rejectFirst = true;
    const driver = driverWithAdapter({
      async enqueue(_port, _workspaceId, intents) {
        attempts.push(intents.map((intent) => intent.client_operation_id));
        if (rejectFirst) {
          rejectFirst = false;
          throw new Error("请求超时: enqueue");
        }
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          accepted_count: intents.length,
          receipts: intents.map((intent) => receipt(intent.client_operation_id)),
          created_node_ids: {},
        };
      },
      async queryStatus(): Promise<SessionCatalogOperationStatusPage> {
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          catalog_revision: 12,
          items: [],
          unknown_operation_ids: [opId("a")],
        };
      },
    });
    await driver.restore();
    await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);
    expect(driver.current().operations[0].state).toBe("unknown");

    await driver.reconcile();
    expect(attempts).toEqual([[opId("a")], [opId("a")]]);
    expect(driver.current().operations[0].client_operation_id).toBe(opId("a"));
    expect(driver.current().operations[0].state).toBe("accepted");
  });

  test("对账拿到 committed 后按已确认 revision 清理持久条目", async () => {
    const persistence = recordingPort();
    const deleted: string[][] = [];
    const port: CatalogOutboxPersistencePort = {
      ...persistence,
      async delete(_partitionKey, clientOperationIds) {
        deleted.push([...clientOperationIds]);
      },
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence: port,
      adapter: {
        async enqueue(_port, _workspaceId, intents) {
          return {
            workspace_id: BACKEND_WORKSPACE_ID,
            accepted_count: intents.length,
            receipts: intents.map((intent) => receipt(intent.client_operation_id)),
            created_node_ids: {},
          };
        },
        async queryStatus() {
          return {
            workspace_id: BACKEND_WORKSPACE_ID,
            catalog_revision: 12,
            items: [receipt(opId("a"), {
              state: "committed",
              committed_catalog_revision: 12,
              receipt_revision: 42,
            })],
            unknown_operation_ids: [],
          } satisfies SessionCatalogOperationStatusPage;
        },
      },
    });
    await driver.restore();
    await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);
    await driver.reconcile();
    const pruned = await driver.prune(12);
    expect(pruned.operations).toEqual([]);
    expect(deleted).toEqual([[opId("a")]]);
  });

  test("剪枝等待持久删除时保留并发新增的命令", async () => {
    const base = recordingPort();
    const deleteStarted = deferred<void>();
    const releaseDelete = deferred<void>();
    const persistence: CatalogOutboxPersistencePort = {
      ...base,
      async delete(partitionKey, operationIds) {
        deleteStarted.resolve();
        await releaseDelete.promise;
        await base.delete(partitionKey, operationIds);
      },
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter: {
        async enqueue(_port, _workspaceId, intents) {
          return {
            workspace_id: BACKEND_WORKSPACE_ID,
            accepted_count: intents.length,
            receipts: intents.map((intent) => receipt(intent.client_operation_id)),
            created_node_ids: {},
          };
        },
        async queryStatus(_port, _workspaceId, operationIds) {
          return {
            workspace_id: BACKEND_WORKSPACE_ID,
            catalog_revision: 12,
            items: [receipt(operationIds[0], {
              state: "committed",
              committed_catalog_revision: 12,
              receipt_revision: 42,
            })],
            unknown_operation_ids: [],
          };
        },
      },
    });
    await driver.restore();
    await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);
    await driver.reconcile();

    const pruning = driver.prune(12);
    await deleteStarted.promise;
    await driver.applyIntent(opId("b"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);
    releaseDelete.resolve();
    const pruned = await pruning;

    expect(pruned.operations.map((operation) => operation.client_operation_id)).toEqual([opId("b")]);
    const restored = await createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter: {
        async enqueue() { throw new Error("本用例不应重放 outbox"); },
        async queryStatus() { throw new Error("本用例不应查询状态"); },
      },
    }).restore();
    expect(restored.operations.map((operation) => operation.client_operation_id)).toEqual([opId("b")]);
  });

  test("F2：依赖未满足时本轮零入队，必须返回 idle 而非谎报 accepted", async () => {
    // op_2 依赖 op_1；op_1 入队结果未知（unknown），因此 op_2 本轮不可入队。
    const enqueued: string[][] = [];
    const driver = driverWithAdapter({
      async enqueue(_port, _workspaceId, intents) {
        enqueued.push(intents.map((intent) => intent.client_operation_id));
        if (intents.some((intent) => intent.client_operation_id === opId("a"))) {
          throw new Error("请求超时: enqueue");
        }
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          accepted_count: intents.length,
          receipts: intents.map((intent) => receipt(intent.client_operation_id)),
          created_node_ids: {},
        } satisfies SessionCatalogEnqueueResult;
      },
      async queryStatus() {
        throw new Error("本用例不应查询状态");
      },
    });
    await driver.restore();
    expect(await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch)).toBe("unknown");

    const outcome2 = await driver.applyIntent(
      opId("b"),
      { kind: "move_node", targetNodeId: "cnode_2", parentNodeId: null },
      { baseCatalogRevision: 7, expectedRevision: 2, dependsOn: [opId("a")] },
    ).then(({ dispatch }) => dispatch);
    // 本轮队列被依赖挡住，op_2 未入队：不得返回 accepted。
    expect(outcome2).toBe("idle");
    expect(enqueued).toEqual([[opId("a")]]);
    const op2 = driver.current().operations.find(
      (operation) => operation.client_operation_id === opId("b"),
    );
    expect(op2?.state).toBe("persisted");
  });
});

describe("会话目录 outbox 驱动：分区广播", () => {
  test("持久化后广播并让另一个 driver 从 port 重读同一条 operation", async () => {
    const persistence = recordingPort();
    const enqueued: string[][] = [];
    const adapter: SessionCatalogOperationsAdapter = {
      async enqueue(_port, _workspaceId, intents) {
        enqueued.push(intents.map((intent) => intent.client_operation_id));
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          accepted_count: intents.length,
          receipts: intents.map((intent) => receipt(intent.client_operation_id)),
          created_node_ids: {},
        };
      },
      async queryStatus() {
        throw new Error("本用例不应查询状态");
      },
    };
    const broadcaster = { posted: [] as string[], post(key: string) { this.posted.push(key); } };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter,
      broadcast: broadcaster,
    });
    await driver.restore();
    await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);

    // 另一个 tab 用同一持久层重读：看到同一份已持久化 outbox，不产生第二份 node。
    const otherTab = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence,
      adapter,
    });
    const reloaded = await otherTab.restore();
    expect(reloaded.operations.map((operation) => operation.client_operation_id)).toEqual([opId("a")]);
    expect(reloaded.operations[0].client_sequence).toBe(1);
    expect(reloaded.operations[0].state).toBe("accepted");
    expect(reloaded.operations[0].receipt_revision).toBe(1);
    expect(broadcaster.posted.length).toBeGreaterThan(0);
  });

  test("BroadcastChannel 缺席时创建播放器必须响亮失败而不是静默无通知", () => {
    const original = Object.getOwnPropertyDescriptor(globalThis, "BroadcastChannel");
    Reflect.deleteProperty(globalThis, "BroadcastChannel");
    try {
      expect(() => createCatalogOutboxBroadcasterForTest())
        .toThrow("当前运行环境没有 BroadcastChannel");
    } finally {
      if (original) Object.defineProperty(globalThis, "BroadcastChannel", original);
    }
  });
});

describe("会话目录 outbox 驱动：身份切换 fence", () => {
  test("deactivate 中止在途请求并拒绝迟到回包与后续操作", async () => {
    const enqueueStarted = deferred<void>();
    const enqueueResult = deferred<SessionCatalogEnqueueResult>();
    let requestSignal: AbortSignal | undefined;
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence: recordingPort(),
      adapter: {
        enqueue(_port, _workspaceId, _intents, signal) {
          requestSignal = signal;
          enqueueStarted.resolve();
          return enqueueResult.promise;
        },
        async queryStatus() {
          throw new Error("失效 driver 不得查询状态");
        },
      },
    });
    await driver.restore();

    const pendingApply = driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch);
    await enqueueStarted.promise;
    const beforeDeactivation = driver.current();
    driver.deactivate();

    expect(requestSignal?.aborted).toBe(true);
    enqueueResult.resolve({
      workspace_id: BACKEND_WORKSPACE_ID,
      accepted_count: 1,
      receipts: [receipt(opId("a"))],
      created_node_ids: {},
    });
    await expect(pendingApply).rejects.toThrow("principal 已失效");
    expect(driver.current()).toBe(beforeDeactivation);
    await expect(driver.applyIntent(opId("b"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    }).then(({ dispatch }) => dispatch)).rejects.toThrow("principal 已失效");
    await expect(driver.reconcile()).rejects.toThrow("principal 已失效");
  });
});

// 显式引用，避免仅在类型层使用该工厂时被误判为未使用导入。
function createCatalogOutboxBroadcasterForTest() {
  return createCatalogOutboxBroadcaster();
}

describe("会话目录失败对账的派发边界", () => {
  for (const missingOwner of [true, false]) {
    test(`${missingOwner ? "缺少 owner" : "权威刷新失败"}后新独立命令不得绕过未完成的失败对账`, async () => {
      const persistence = recordingPort();
      const enqueued: string[][] = [];
      let refreshAvailable = false;
      const driver = createSessionCatalogOutboxDriver({
        port: PORT, gatewayWorkspaceId: GATEWAY_WORKSPACE_ID, workspaceTarget: WORKSPACE_TARGET,
        partition: PARTITION, persistence,
        reconcileFailure: missingOwner ? undefined : async (_reported, revision, finalize) => {
          expect(revision).toBe(12);
          if (!refreshAvailable) throw new Error("权威目录读取失败");
          await finalize();
        },
        adapter: {
          async enqueue(_port, _target, intents) {
            enqueued.push(intents.map((intent) => intent.client_operation_id));
            return { workspace_id: BACKEND_WORKSPACE_ID, accepted_count: intents.length,
              receipts: intents.map((intent) => receipt(intent.client_operation_id)), created_node_ids: {} };
          },
          async queryStatus(_port, _target, ids) {
            return { workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: 12,
              items: ids.includes(opId("a")) ? [receipt(opId("a"), { state: "rejected",
                error_code: "revision_changed", error_detail: "其它客户端已修改 A", receipt_revision: 2 })] : [],
              unknown_operation_ids: ids.filter((id) => id !== opId("a")) };
          },
        },
      });
      await driver.restore();
      await driver.applyIntent(opId("a"), renameIntent(), { baseCatalogRevision: 7, expectedRevision: 3 }).then(({ dispatch }) => dispatch);
      const failure = missingOwner ? "缺少 confirmed" : "权威目录读取失败";
      await expect(driver.reconcile()).rejects.toThrow(failure);
      await expect(driver.applyIntent(opId("c"), { kind: "rename_node", targetNodeId: "cnode_3", name: "独立 C" },
        { baseCatalogRevision: 7, expectedRevision: 4 }).then(({ dispatch }) => dispatch)).rejects.toThrow(failure);
      expect(enqueued).toEqual([[opId("a")]]);
      expect(driver.current().operations.map((operation) => operation.client_operation_id)).toEqual([opId("a"), opId("c")]);
      expect((await persistence.load(PARTITION_KEY)).map((record) => record.operation.client_operation_id)).toEqual([opId("a"), opId("c")]);
      expect(driver.current().operations[1].base_catalog_revision).toBe(7);
      if (!missingOwner) {
        refreshAvailable = true;
        await driver.reconcile();
        expect(enqueued).toEqual([[opId("a")], [opId("c")]]);
        expect(driver.current().operations.map((operation) => operation.client_operation_id)).toEqual([opId("c")]);
        expect(driver.current().operations[0].base_catalog_revision).toBe(7);
      }
    });
  }

  test("入队返回已有拒绝 tombstone 时先等权威对账并如实返回 rejected", async () => {
    const refreshing = deferred();
    const release = deferred();
    let driver!: CatalogOutboxDriver;
    driver = createSessionCatalogOutboxDriver({
      port: PORT, gatewayWorkspaceId: GATEWAY_WORKSPACE_ID, workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION, persistence: recordingPort(),
      async reconcileFailure(reported, _revision, finalize) {
        expect(reported.operations[0].state).toBe("rejected");
        refreshing.resolve();
        await release.promise;
        await finalize();
      },
      adapter: {
        async enqueue() {
          return { workspace_id: BACKEND_WORKSPACE_ID, accepted_count: 1,
            receipts: [receipt(opId("a"), { state: "rejected", receipt_revision: 2,
              error_code: "revision_changed", error_detail: "原操作已被拒绝" })], created_node_ids: {} };
        },
        async queryStatus() { throw new Error("202 已返回完整终态，不能改用第二次读取取得同一回执"); },
      },
    });
    await driver.restore();
    const applying = driver.applyIntent(opId("a"), renameIntent(), { baseCatalogRevision: 7, expectedRevision: 3 }).then(({ dispatch }) => dispatch);
    try {
      await refreshing.promise;
      expect(driver.current().operations[0].state).toBe("persisted");
    } finally { release.resolve(); }
    expect(await applying).toBe("rejected");
    expect(driver.current().operations).toEqual([]);
  });

  test("身份切换取消失败刷新，迟到 owner 不得清理持久 pending", async () => {
    const persistence = recordingPort();
    const refreshing = deferred();
    const release = deferred();
    let refreshSignal!: AbortSignal;
    const driver = createSessionCatalogOutboxDriver({
      port: PORT, gatewayWorkspaceId: GATEWAY_WORKSPACE_ID, workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION, persistence,
      async reconcileFailure(_reported, _revision, finalize, signal) {
        refreshSignal = signal;
        refreshing.resolve();
        await release.promise;
        await finalize();
      },
      adapter: {
        async enqueue() { return { workspace_id: BACKEND_WORKSPACE_ID, accepted_count: 1,
          receipts: [receipt(opId("a"))], created_node_ids: {} }; },
        async queryStatus() { return { workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: 12,
          items: [receipt(opId("a"), { state: "cancelled", error_detail: "服务端已取消", receipt_revision: 2 })],
          unknown_operation_ids: [] }; },
      },
    });
    await driver.restore();
    await driver.applyIntent(opId("a"), renameIntent(), { baseCatalogRevision: 7, expectedRevision: 3 }).then(({ dispatch }) => dispatch);
    const reconciling = driver.reconcile();
    await refreshing.promise;
    const current = driver.current();
    driver.deactivate();
    expect(refreshSignal.aborted).toBe(true);
    release.resolve();
    await expect(reconciling).rejects.toThrow("principal 已失效");
    expect(driver.current()).toBe(current);
    expect((await persistence.load(PARTITION_KEY)).map((record) => record.operation.client_operation_id)).toEqual([opId("a")]);
  });
});

describe("跨 tab 失败对账", () => {
  test("外部删项通知先按原 ID 对账与刷新 confirmed，不能先丢失败信号", async () => {
    const persistence = recordingPort();
    const refreshing = deferred();
    const release = deferred();
    let enqueues = 0;
    const driver = createSessionCatalogOutboxDriver({
      port: PORT, gatewayWorkspaceId: GATEWAY_WORKSPACE_ID, workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION, persistence,
      async reconcileFailure(_reported, revision, finalize) {
        expect(revision).toBe(12);
        refreshing.resolve();
        await release.promise;
        await finalize();
      },
      adapter: {
        async enqueue(_port, _target, intents) {
          enqueues += 1;
          return { workspace_id: BACKEND_WORKSPACE_ID, accepted_count: intents.length,
            receipts: intents.map((intent) => receipt(intent.client_operation_id)), created_node_ids: {} };
        },
        async queryStatus(_port, _target, ids) {
          expect(ids).toEqual([opId("a"), opId("c")]);
          return { workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: 12,
            items: [receipt(opId("a"), { state: "dependency_failed", receipt_revision: 2, error_detail: "其它窗口已对账失败" }),
              receipt(opId("c"))], unknown_operation_ids: [] };
        },
      },
    });
    await driver.restore();
    await driver.applyIntent(opId("a"), renameIntent(), { baseCatalogRevision: 7, expectedRevision: 3 }).then(({ dispatch }) => dispatch);
    await driver.applyIntent(opId("c"), { kind: "rename_node", targetNodeId: "cnode_3", name: "C" }, { baseCatalogRevision: 7, expectedRevision: 4 }).then(({ dispatch }) => dispatch);
    await persistence.delete(PARTITION_KEY, [opId("a")]);
    const reloading = driver.reload();
    try {
      await Promise.race([refreshing.promise, reloading.then(() => { throw new Error("外部删项尚未完成 confirmed 刷新就已公开"); })]);
      expect(driver.current().operations.map((operation) => operation.client_operation_id)).toEqual([opId("a"), opId("c")]);
    } finally { release.resolve(); }
    await reloading;
    expect(enqueues).toBe(2);
    expect(driver.current().operations.map((operation) => operation.client_operation_id)).toEqual([opId("c")]);
    expect(driver.current().operations[0].state).toBe("accepted");
    expect(driver.current().operations[0].base_catalog_revision).toBe(7);
  });
});


describe("失败持久清理的同 driver 恢复", () => {
  test("持久清理失败后同 driver 重试，并保留清理等待中新独立命令及原派发身份", async () => {
    const base = recordingPort();
    let seed = createCatalogOutbox(PARTITION);
    seed = addCatalogOutboxIntent(seed, opId("a"), {
      kind: "create_folder", name: "被拒绝的父目录 A",
    }, { baseCatalogRevision: 7 });
    seed = addCatalogOutboxIntent(seed, opId("b"), {
      kind: "move_node", targetNodeId: "cnode_b", parentCreatedByOperationId: opId("a"),
    }, { baseCatalogRevision: 7, expectedRevision: 2, dependsOn: [opId("a")] });
    seed = addCatalogOutboxIntent(seed, opId("c"), renameIntent(), {
      baseCatalogRevision: 7, expectedRevision: 3,
    });
    for (const operation of seed.operations) await base.insert(PARTITION_KEY, operation);
    const retryDeleteStarted = deferred<void>();
    const releaseDelete = deferred<void>();
    const newIntentsPersisted = deferred<void>();
    const deleted: string[][] = [];
    const phases: string[] = [];
    const enqueued: ReturnType<typeof catalogOutboxBatchToIntents>[] = [];
    let queryCount = 0;
    let ownerCount = 0;
    let driver!: CatalogOutboxDriver;
    const persistence: CatalogOutboxPersistencePort = {
      ...base,
      async delete(partitionKey, ids) {
        deleted.push([...ids]);
        phases.push("delete:" + ids.join(","));
        if (deleted.length === 1) throw new Error("AbortError: IndexedDB 删除事务暂时失败");
        if (deleted.length === 2) {
          retryDeleteStarted.resolve();
          await releaseDelete.promise;
        }
        await base.delete(partitionKey, ids);
      },
    };
    driver = createSessionCatalogOutboxDriver({
      port: PORT, gatewayWorkspaceId: GATEWAY_WORKSPACE_ID, workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION, persistence,
      async reconcileFailure(reported, revision, finalize, signal) {
        ownerCount += 1;
        expect(revision).toBe(12);
        expect(reported.operations.find((operation) => operation.client_operation_id === opId("a"))?.state)
          .toBe("rejected");
        expect(signal.aborted).toBe(false);
        phases.push("confirmed:" + ownerCount, "projection:before");
        await finalize();
        phases.push("projection:after");
        expect(driver.current().operations.every((operation) =>
          operation.client_operation_id === opId("c") || operation.client_operation_id === opId("e")))
          .toBe(true);
      },
      adapter: {
        async queryStatus(_port, target, ids) {
          queryCount += 1;
          expect(target).toEqual(WORKSPACE_TARGET);
          expect(ids).toEqual([opId("a"), opId("b"), opId("c")]);
          return { workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: 12,
            items: [receipt(opId("a"), { kind: "create_folder", state: "rejected",
              error_code: "revision_changed", error_detail: "父目录被其它客户端修改", receipt_revision: 2 })],
            unknown_operation_ids: [opId("b"), opId("c")] };
        },
        async enqueue(_port, target, intents) {
          expect(target).toEqual(WORKSPACE_TARGET);
          expect(phases[phases.length - 1]).toBe("projection:after");
          phases.push("enqueue");
          enqueued.push(structuredClone(intents));
          return { workspace_id: BACKEND_WORKSPACE_ID, accepted_count: intents.length,
            receipts: intents.map((intent) => receipt(intent.client_operation_id, {
              client_sequence: intent.client_sequence,
            })), created_node_ids: {} };
        },
      },
      onChange(outbox) {
        const newIds = [opId("d"), opId("e")];
        if (newIds.every((id) => outbox.operations.some((operation) =>
          operation.client_operation_id === id && operation.state === "persisted"))) {
          newIntentsPersisted.resolve();
        }
      },
    });
    await driver.restore();
    const originalC = structuredClone(driver.current().operations.find((operation) =>
      operation.client_operation_id === opId("c"))!);
    const originalWire = catalogOutboxBatchToIntents([originalC]);
    await expect(driver.reconcile()).rejects.toThrow("IndexedDB 删除事务暂时失败");
    expect(enqueued).toEqual([]);
    expect((await persistence.load(PARTITION_KEY)).map((record) => record.operation.client_operation_id))
      .toEqual([opId("a"), opId("b"), opId("c")]);

    // 必须复用原 driver 与原回执；第一次 delete 失败不能要求整页恢复或重查新 ID。
    const retrying = driver.reconcile();
    let applying: Promise<PromiseSettledResult<unknown>[]> | null = null;
    let originalE: CatalogOutboxOperation | null = null;
    try {
      await Promise.race([retryDeleteStarted.promise, retrying.then(() => {
        throw new Error("持久清理重试尚未开始就已返回成功");
      })]);
      expect(driver.current().operations.map((operation) => operation.client_operation_id))
        .toEqual([opId("a"), opId("b"), opId("c")]);
      expect(driver.current().operations[0]!.state).toBe("rejected");
      const dependent = driver.applyIntent(opId("d"), {
        kind: "create_folder", name: "清理期间新增的依赖 D",
      }, { baseCatalogRevision: 7, dependsOn: [opId("b")] }).then(({ dispatch }) => dispatch);
      const independent = driver.applyIntent(opId("e"), {
        kind: "rename_node", targetNodeId: "cnode_e", name: "清理期间新增的独立 E",
      }, { baseCatalogRevision: 7, expectedRevision: 5 }).then(({ dispatch }) => dispatch);
      applying = Promise.allSettled([dependent, independent]);
      await newIntentsPersisted.promise;
      originalE = structuredClone(driver.current().operations.find((operation) =>
        operation.client_operation_id === opId("e"))!);
      expect(catalogOutboxBatchToIntents([driver.current().operations.find((operation) =>
        operation.client_operation_id === opId("c"))!])).toEqual(originalWire);
      expect(enqueued).toEqual([]);
      expect((await persistence.load(PARTITION_KEY)).map((record) => record.operation.client_operation_id))
        .toEqual([opId("a"), opId("b"), opId("c"), opId("d"), opId("e")]);
    } finally {
      releaseDelete.resolve();
      await Promise.allSettled([retrying, ...(applying ? [applying] : [])]);
    }
    await retrying;
    expect(await applying!).toEqual([
      { status: "fulfilled", value: "rejected" },
      { status: "fulfilled", value: "accepted" },
    ]);
    expect(queryCount).toBe(1);
    expect(ownerCount).toBe(2);
    expect(deleted).toEqual([[opId("a"), opId("b")], [opId("a"), opId("b")], [opId("d")]]);
    expect(phases).toEqual([
      "confirmed:1", "projection:before", "delete:" + [opId("a"), opId("b")].join(","),
      "confirmed:2", "projection:before", "delete:" + [opId("a"), opId("b")].join(","),
      "delete:" + opId("d"), "projection:after", "enqueue",
    ]);
    expect(enqueued).toEqual([catalogOutboxBatchToIntents([originalC, originalE!])]);
    expect(driver.current().operations.map((operation) => operation.client_operation_id))
      .toEqual([opId("c"), opId("e")]);
    expect(driver.current().operations.map((operation) => operation.state)).toEqual(["accepted", "accepted"]);
    expect(catalogOutboxBatchToIntents(driver.current().operations))
      .toEqual(catalogOutboxBatchToIntents([originalC, originalE!]));
    expect((await persistence.load(PARTITION_KEY)).map((record) => record.operation.client_operation_id))
      .toEqual([opId("c"), opId("e")]);
  });

  test("持久删除等待中失效不得发布清理成功或继续派发", async () => {
    const base = recordingPort();
    const deleting = deferred<void>();
    const release = deferred<void>();
    const phases: string[] = [];
    const broadcasts: string[] = [];
    let failureSignal!: AbortSignal;
    const driver = createSessionCatalogOutboxDriver({
      port: PORT, gatewayWorkspaceId: GATEWAY_WORKSPACE_ID, workspaceTarget: WORKSPACE_TARGET,
      partition: PARTITION,
      persistence: { ...base, async delete(partitionKey, ids) {
        deleting.resolve();
        await release.promise;
        await base.delete(partitionKey, ids);
      } },
      broadcast: { post: (key) => broadcasts.push(key) },
      async reconcileFailure(_reported, _revision, finalize, signal) {
        failureSignal = signal;
        phases.push("projection:before");
        await finalize();
        phases.push("projection:after");
      },
      adapter: {
        async enqueue() { return { workspace_id: BACKEND_WORKSPACE_ID, accepted_count: 1,
          receipts: [receipt(opId("a"))], created_node_ids: {} }; },
        async queryStatus() { return { workspace_id: BACKEND_WORKSPACE_ID, catalog_revision: 12,
          items: [receipt(opId("a"), { state: "cancelled", error_detail: "服务端已取消", receipt_revision: 2 })],
          unknown_operation_ids: [] }; },
      },
    });
    await driver.restore();
    await driver.applyIntent(opId("a"), renameIntent(), { baseCatalogRevision: 7, expectedRevision: 3 }).then(({ dispatch }) => dispatch);
    const reconciling = driver.reconcile();
    await deleting.promise;
    const beforeDeactivation = driver.current();
    const broadcastCount = broadcasts.length;
    driver.deactivate();
    expect(failureSignal.aborted).toBe(true);
    release.resolve();
    await expect(reconciling).rejects.toThrow("principal 已失效");
    expect(driver.current()).toBe(beforeDeactivation);
    expect(driver.current().operations.map((operation) => operation.client_operation_id)).toEqual([opId("a")]);
    expect(driver.current().operations[0]!.state).toBe("cancelled");
    expect(broadcasts).toHaveLength(broadcastCount);
    expect(phases).toEqual(["projection:before"]);
    // 已开始的持久事务可能完成，但旧 principal 不能把它伪报为 UI 清理成功。
    expect(await base.load(PARTITION_KEY)).toEqual([]);
  });
});

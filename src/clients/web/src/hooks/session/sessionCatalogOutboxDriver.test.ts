import { describe, expect, test } from "bun:test";
import { HttpRequestError } from "../../api/http";
import type {
  SessionCatalogEnqueueResult,
  SessionCatalogOperationReceipt,
  SessionCatalogOperationStatusPage,
} from "../../api/session/sessionCatalogOperations";
import {
  addCatalogOutboxIntent,
  assignCatalogOutboxOperationSequence,
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
    pending_settlement: false,
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
  test("同步 pending 立即可见，随后落盘并批量入队", async () => {
    const events: string[] = [];
    const enqueued: string[][] = [];
    const routedWorkspaceIds: string[] = [];
    const queriedWorkspaceIds: string[] = [];
    const persistence = recordingPort();
    const adapter: SessionCatalogOperationsAdapter = {
      async enqueue(_port, workspaceId, intents) {
        routedWorkspaceIds.push(workspaceId);
        events.push("enqueue");
        enqueued.push(intents.map((intent) => intent.client_operation_id));
        return {
          workspace_id: BACKEND_WORKSPACE_ID,
          accepted_count: intents.length,
          receipts: intents.map((intent) => receipt(intent.client_operation_id)),
          created_node_ids: {},
        } satisfies SessionCatalogEnqueueResult;
      },
      async queryStatus(_port, workspaceId, operationIds) {
        queriedWorkspaceIds.push(workspaceId);
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
    });

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
    expect(routedWorkspaceIds).toEqual([GATEWAY_WORKSPACE_ID]);
    expect((await persistence.load(PARTITION_KEY))
      .map((record) => record.operation.client_operation_id)).toEqual([opId("a")]);
    expect(await persistence.load(
      `${PARTITION.gatewayId}\u0000${GATEWAY_WORKSPACE_ID}\u0000${PARTITION.principal}`,
    )).toEqual([]);
    expect(driver.current().operations[0].state).toBe("accepted");

    await driver.reconcile();
    expect(queriedWorkspaceIds).toEqual([GATEWAY_WORKSPACE_ID]);
  });

  test("本地持久化失败时撤销 pending、显示错误且绝不派发入队", async () => {
    let enqueueCount = 0;
    const persistence: CatalogOutboxPersistencePort = {
      load: async () => [],
      insert: async () => {
        throw new Error("QuotaExceededError: 本地存储已满");
      },
      delete: async () => undefined,
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
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
    })).rejects.toThrow("会话目录 pending 变更未能本地持久化，已撤销该操作及依赖");

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
      partition: PARTITION,
      persistence,
      adapter,
    });
    await driver.restore();
    await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    });

    failNextInsert = true;
    await expect(driver.applyIntent(opId("b"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    })).rejects.toThrow("已撤销该操作及依赖");

    // 磁盘上不得留下失败命令，刷新恢复后也不能被重新派发。
    expect([...disk.values()].map((operation) => operation.client_operation_id))
      .not.toContain(opId("b"));
    const resumed = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
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
    });
    expect(enqueued.slice(before)).not.toContain(opId("b"));
  });

  test("刷新恢复 outbox 后未对账命令仍保留并可继续入队", async () => {
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
      partition: PARTITION,
      persistence,
      adapter,
    });
    await first.restore();
    await first.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    });

    // 模拟刷新：新 driver 从同一持久层恢复，序号与 operation ID 都来自原记录。
    expect(persistence.writes.map((operation) => operation.client_operation_id)).toEqual([opId("a")]);
    expect(enqueued).toEqual([[opId("a")]]);
    const resumed = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      partition: PARTITION,
      persistence,
      adapter,
    });
    const restored = await resumed.restore();
    expect(restored.operations.map((operation) => operation.client_operation_id)).toEqual([opId("a")]);
    expect(restored.operations[0].state).toBe("persisted");
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
      partition: PARTITION,
      persistence,
      adapter,
    });
    await driver.restore();

    const otherTab = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      partition: PARTITION,
      persistence,
      adapter,
    });
    await otherTab.restore();
    await otherTab.applyIntent(opId("b"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    });

    delayNextLoad = true;
    const reloading = driver.reload();
    await loadStarted.promise;
    await driver.applyIntent(opId("c"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    });
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
  ): CatalogOutboxDriver {
    return createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      partition: PARTITION,
      persistence: recordingPort(),
      adapter,
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
    });
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
    });
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
    });
    await driver.restore();
    const outcome = await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    });
    expect(outcome).toBe("rejected");
    expect(driver.current().operations).toEqual([]);
  });

  test("拒绝对账与未完成 insert 交错时，依赖闭包和独立命令在磁盘与内存一致", async () => {
    const base = recordingPort();
    const bInserted = deferred<void>();
    const releaseBInsert = deferred<void>();
    const deleteStarted = deferred<void>();
    const bCleanupStarted = deferred<void>();
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
        else bCleanupStarted.resolve();
        await releaseDelete.promise;
        await base.delete(partitionKey, clientOperationIds);
      },
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      partition: PARTITION,
      persistence,
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
    }, { baseCatalogRevision: 7 });
    await enqueueStarted.promise;
    const dependent = driver.applyIntent(opId("b"), {
      kind: "move_node",
      targetNodeId: "cnode_2",
      parentCreatedByOperationId: opId("a"),
    }, { baseCatalogRevision: 7, expectedRevision: 2, dependsOn: [opId("a")] });
    await bInserted.promise;
    const independent = driver.applyIntent(opId("c"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    });
    await cPersisted.promise;

    rejectA.resolve();
    await deleteStarted.promise;
    // B 的 IDB 行已提交，但它的 insert continuation 尚未把序号回填到内存。
    releaseBInsert.resolve();
    await bCleanupStarted.promise;
    releaseDelete.resolve();
    const outcomes = await Promise.allSettled([first, dependent, independent]);
    expect(outcomes[0]).toMatchObject({ status: "fulfilled", value: "rejected" });
    expect(outcomes[1]).toMatchObject({ status: "rejected" });
    expect(outcomes[2]).toMatchObject({ status: "fulfilled", value: "accepted" });
    expect(deleteCalls[0]).toEqual([opId("a"), opId("b")]);
    expect(enqueueCalls).toEqual([[opId("a")], [opId("c")]]);

    expect(driver.current().operations.map((operation) => operation.client_operation_id))
      .toEqual([opId("c")]);
    expect(driver.current().operations[0].state).toBe("accepted");
    const restored = await createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      partition: PARTITION,
      persistence,
      adapter: {
        async enqueue() { throw new Error("本用例不应重放 outbox"); },
        async queryStatus() { throw new Error("本用例不应查询状态"); },
      },
    }).restore();
    expect(restored.operations.map((operation) => operation.client_operation_id))
      .toEqual([opId("c")]);
    expect(restored.operations[0].state).toBe("persisted");
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
    });
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
              receipt_revision: 12,
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
    });
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
              receipt_revision: 12,
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
    });
    await driver.reconcile();

    const pruning = driver.prune(12);
    await deleteStarted.promise;
    await driver.applyIntent(opId("b"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    });
    releaseDelete.resolve();
    const pruned = await pruning;

    expect(pruned.operations.map((operation) => operation.client_operation_id)).toEqual([opId("b")]);
    const restored = await createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
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
    })).toBe("unknown");

    const outcome2 = await driver.applyIntent(
      opId("b"),
      { kind: "move_node", targetNodeId: "cnode_2", parentNodeId: null },
      { baseCatalogRevision: 7, expectedRevision: 2, dependsOn: [opId("a")] },
    );
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
      partition: PARTITION,
      persistence,
      adapter,
      broadcast: broadcaster,
    });
    await driver.restore();
    await driver.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    });

    // 另一个 tab 用同一持久层重读：看到同一份已持久化 outbox，不产生第二份 node。
    const otherTab = createSessionCatalogOutboxDriver({
      port: PORT,
      gatewayWorkspaceId: GATEWAY_WORKSPACE_ID,
      partition: PARTITION,
      persistence,
      adapter,
    });
    const reloaded = await otherTab.restore();
    expect(reloaded.operations.map((operation) => operation.client_operation_id)).toEqual([opId("a")]);
    expect(reloaded.operations[0].client_sequence).toBe(1);
    expect(reloaded.operations[0].state).toBe("persisted");
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

// 显式引用，避免仅在类型层使用该工厂时被误判为未使用导入。
function createCatalogOutboxBroadcasterForTest() {
  return createCatalogOutboxBroadcaster();
}

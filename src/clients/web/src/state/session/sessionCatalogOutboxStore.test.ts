import { describe, expect, test } from "bun:test";
import {
  addCatalogOutboxIntent,
  createCatalogOutbox,
  type CatalogOutboxOperation,
} from "./sessionCatalogOutbox";
import {
  deleteCatalogOutboxOperations,
  loadCatalogOutbox,
  persistCatalogOutboxPendingOperation,
  type CatalogOutboxPersistencePort,
  type CatalogOutboxStoredOperation,
} from "./sessionCatalogOutboxStore";

const PARTITION = { gatewayId: "local:8014", workspaceId: "workspace-1", principal: "guest" };
const PARTITION_KEY = "local:8014\u0000workspace-1\u0000guest";
const OTHER_PARTITION = { ...PARTITION, principal: "user_b" };

function opId(suffix: string): string {
  return "op_" + suffix.padStart(32, "0");
}

function pendingRename(operationId: string, name = "新名字"): CatalogOutboxOperation {
  return addCatalogOutboxIntent(
    createCatalogOutbox(PARTITION),
    operationId,
    { kind: "rename_node", targetNodeId: "node-1", name },
    { baseCatalogRevision: 7, expectedRevision: 3 },
  ).operations[0];
}

/** 端口单测桩；不模拟跨 tab 隔离，真实事务语义由 Chromium 集成测试覆盖。 */
function memoryPort(): CatalogOutboxPersistencePort {
  const records = new Map<string, Map<number, CatalogOutboxStoredOperation>>();
  const nextSequence = new Map<string, number>();
  return {
    async load(partitionKey) {
      return [...(records.get(partitionKey)?.values() ?? [])];
    },
    async insert(partitionKey, operation) {
      if (operation.client_sequence !== null) {
        throw new Error("memory port 只接收尚未赋号的 operation");
      }
      const bucket = records.get(partitionKey) ?? new Map();
      if ([...bucket.values()].some(
        (record) => record.operation.client_operation_id === operation.client_operation_id,
      )) {
        throw new Error("duplicate operation ID");
      }
      const sequence = nextSequence.get(partitionKey) ?? 1;
      const stored: CatalogOutboxStoredOperation = {
        partition_key: partitionKey,
        client_sequence: sequence,
        operation: { ...operation, client_sequence: sequence },
      };
      bucket.set(sequence, stored);
      records.set(partitionKey, bucket);
      nextSequence.set(partitionKey, sequence + 1);
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

describe("会话目录 outbox 持久层端口", () => {
  test("事务端口为同一分区依次分配 canonical sequence，restore 不改写持久事实", async () => {
    const port = memoryPort();
    const first = await persistCatalogOutboxPendingOperation(
      port,
      PARTITION,
      pendingRename(opId("a")),
    );
    const second = await persistCatalogOutboxPendingOperation(
      port,
      PARTITION,
      pendingRename(opId("b"), "第二个"),
    );
    const restored = await loadCatalogOutbox(port, PARTITION);

    expect([first.client_sequence, second.client_sequence]).toEqual([1, 2]);
    expect(restored.operations.map((operation) => operation.client_sequence)).toEqual([1, 2]);
    expect(restored.operations.map((operation) => operation.state)).toEqual(["persisted", "persisted"]);
  });

  test("不同 principal 分区互不可见且分别从 1 分配", async () => {
    const port = memoryPort();
    await persistCatalogOutboxPendingOperation(port, PARTITION, pendingRename(opId("a")));
    const otherOperation = addCatalogOutboxIntent(
      createCatalogOutbox(OTHER_PARTITION),
      opId("b"),
      { kind: "rename_node", targetNodeId: "node-2", name: "用户 B" },
      { baseCatalogRevision: 7, expectedRevision: 1 },
    ).operations[0];
    const other = await persistCatalogOutboxPendingOperation(port, OTHER_PARTITION, otherOperation);

    expect(other.client_sequence).toBe(1);
    expect((await loadCatalogOutbox(port, OTHER_PARTITION)).operations).toEqual([
      { ...other.operation, state: "persisted" },
    ]);
  });

  test("清理最高序号 operation 后持久高水位不回退", async () => {
    const port = memoryPort();
    const first = await persistCatalogOutboxPendingOperation(
      port,
      PARTITION,
      pendingRename(opId("a")),
    );
    const highest = await persistCatalogOutboxPendingOperation(
      port,
      PARTITION,
      pendingRename(opId("b"), "最高"),
    );
    await deleteCatalogOutboxOperations(
      port,
      PARTITION,
      [highest.operation.client_operation_id],
    );
    const next = await persistCatalogOutboxPendingOperation(
      port,
      PARTITION,
      pendingRename(opId("c"), "清理后"),
    );

    expect(first.client_sequence).toBe(1);
    expect(highest.client_sequence).toBe(2);
    expect(next.client_sequence).toBe(3);
  });

  test("重复 operation ID 的插入失败且原命令不变", async () => {
    const port = memoryPort();
    const original = await persistCatalogOutboxPendingOperation(
      port,
      PARTITION,
      pendingRename(opId("a"), "原命令"),
    );
    await expect(persistCatalogOutboxPendingOperation(
      port,
      PARTITION,
      pendingRename(opId("a"), "冲突命令"),
    )).rejects.toThrow("duplicate operation ID");

    const restored = await loadCatalogOutbox(port, PARTITION);
    expect(restored.operations).toEqual([{ ...original.operation, state: "persisted" }]);
    expect(restored.operations[0].name).toBe("原命令");
  });

  test("加载时拒绝不匹配的分区或 sequence 主键", async () => {
    const operation = pendingRename(opId("a"));
    const wrongPartition: CatalogOutboxPersistencePort = {
      load: async () => [{
        partition_key: "wrong",
        client_sequence: 1,
        operation: { ...operation, client_sequence: 1 },
      }],
      insert: async () => { throw new Error("unexpected insert"); },
      delete: async () => undefined,
    };
    await expect(loadCatalogOutbox(wrongPartition, PARTITION))
      .rejects.toThrow("outbox 记录的分区键与请求不符");

    const wrongSequence: CatalogOutboxPersistencePort = {
      load: async () => [{
        partition_key: PARTITION_KEY,
        client_sequence: 9,
        operation: { ...operation, client_sequence: 1 },
      }],
      insert: async () => { throw new Error("unexpected insert"); },
      delete: async () => undefined,
    };
    await expect(loadCatalogOutbox(wrongSequence, PARTITION))
      .rejects.toThrow("outbox 记录主键与 operation 序号不一致");
  });

  test("首次持久化失败必须原样显式抛出", async () => {
    const failing: CatalogOutboxPersistencePort = {
      load: async () => [],
      insert: async () => {
        throw new Error("QuotaExceededError: 本地存储已满");
      },
      delete: async () => undefined,
    };
    await expect(persistCatalogOutboxPendingOperation(
      failing,
      PARTITION,
      pendingRename(opId("a")),
    )).rejects.toThrow("QuotaExceededError: 本地存储已满");
  });
});

import { afterEach, describe, expect, test } from "bun:test";
import {
  addCatalogOutboxIntent,
  createCatalogOutbox,
  markCatalogOutboxOperationPersisted,
} from "./sessionCatalogOutbox";
import {
  createIndexedDbCatalogOutboxPort,
  deleteCatalogOutboxOperations,
  openCatalogOutboxDatabase,
  loadCatalogOutbox,
  persistCatalogOutboxPendingOperations,
  type CatalogOutboxPersistencePort,
} from "./sessionCatalogOutboxStore";
import { createIndexedDbFake } from "./sessionCatalogOutboxIdbFake";

const PARTITION = { gatewayId: "local:8014", workspaceId: "workspace-1", principal: "guest" };
const OTHER_PARTITION = { ...PARTITION, principal: "user_b" };

function opId(suffix: string): string {
  return `op_${suffix.padStart(32, "0")}`;
}

function outboxWithRename() {
  return addCatalogOutboxIntent(
    createCatalogOutbox(PARTITION),
    opId("a"),
    { kind: "rename_node", targetNodeId: "node-1", name: "新名字" },
    { baseCatalogRevision: 7, expectedRevision: 3 },
  );
}

// 每个用例前装入干净的桩 IndexedDB：进程级同名数据库会保留数据，因此必须显式换新。
function installIndexedDbFake(): void {
  Object.defineProperty(globalThis, "indexedDB", {
    configurable: true,
    value: createIndexedDbFake(),
  });
}

installIndexedDbFake();

/** 只实现本端口三方法的确定性内存桩：用于验证端口契约本身，不模拟 IndexedDB 语义。 */
function memoryPort(): CatalogOutboxPersistencePort {
  const records = new Map<string, Map<number, StoredRecord>>();
  type StoredRecord = Parameters<CatalogOutboxPersistencePort["write"]>[0][number];
  return {
    async load(partitionKey) {
      return [...(records.get(partitionKey)?.values() ?? [])];
    },
    async write(batch) {
      for (const record of batch) {
        const bucket = records.get(record.partition_key) ?? new Map();
        bucket.set(record.client_sequence, record);
        records.set(record.partition_key, bucket);
      }
    },
    async delete(partitionKey, clientSequences) {
      const bucket = records.get(partitionKey);
      if (!bucket) return;
      for (const clientSequence of clientSequences) bucket.delete(clientSequence);
    },
  };
}

afterEach(async () => {
  // 桩数据库是进程级的：每个用例结束后换新，避免用例之间互相看到对方的 pending。
  installIndexedDbFake();
});

describe("会话目录 outbox 持久层：按序写入与恢复", () => {
  test("未落盘的 pending 写入后可按分区整体读回", async () => {
    const port = memoryPort();
    const outbox = outboxWithRename();
    await persistCatalogOutboxPendingOperations(port, PARTITION, outbox.operations);
    const restored = await loadCatalogOutbox(port, PARTITION);
    expect(restored.operations.map((item) => item.client_operation_id)).toEqual([opId("a")]);
    expect(restored.operations[0].state).toBe("pending_local");
    expect(restored.next_client_sequence).toBe(2);
  });

  test("按 client_sequence 有序写入，读回顺序与本地一致", async () => {
    const port = memoryPort();
    let outbox = outboxWithRename();
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("b"),
      { kind: "rename_node", targetNodeId: "node-2", name: "第二个" },
      { baseCatalogRevision: 7, expectedRevision: 1 },
    );
    // 故意乱序提交：持久层必须按 client_sequence 落盘。
    await persistCatalogOutboxPendingOperations(port, PARTITION, [...outbox.operations].reverse());
    const restored = await loadCatalogOutbox(port, PARTITION);
    expect(restored.operations.map((item) => item.client_sequence)).toEqual([1, 2]);
    expect(restored.operations.map((item) => item.client_operation_id))
      .toEqual([opId("a"), opId("b")]);
  });

  test("不同 principal 分区互不可见", async () => {
    const port = memoryPort();
    await persistCatalogOutboxPendingOperations(port, PARTITION, outboxWithRename().operations);
    const other = await loadCatalogOutbox(port, OTHER_PARTITION);
    expect(other.operations).toEqual([]);
    expect(other.next_client_sequence).toBe(1);
  });

  test("恢复后继续登记不会复用已用过的 client_sequence", async () => {
    const port = memoryPort();
    await persistCatalogOutboxPendingOperations(port, PARTITION, outboxWithRename().operations);
    const restored = await loadCatalogOutbox(port, PARTITION);
    const continued = addCatalogOutboxIntent(
      restored,
      opId("b"),
      { kind: "rename_node", targetNodeId: "node-2", name: "第二个" },
      { baseCatalogRevision: 7, expectedRevision: 1 },
    );
    expect(continued.operations.map((item) => item.client_sequence)).toEqual([1, 2]);
    expect(continued.next_client_sequence).toBe(3);
  });

  test("已对账条目删除后不再被恢复", async () => {
    const port = memoryPort();
    await persistCatalogOutboxPendingOperations(port, PARTITION, outboxWithRename().operations);
    await deleteCatalogOutboxOperations(port, PARTITION, [1]);
    const restored = await loadCatalogOutbox(port, PARTITION);
    expect(restored.operations).toEqual([]);
  });

  test("分区键与记录主键不一致时必须响亮失败", async () => {
    const outbox = outboxWithRename();
    // 契约破坏场景：持久层把不属于本分区的记录返回给调用方（索引/键错配的典型症状）。
    // 读取方必须响亮失败，而不是把别的分区的 pending 串进当前投影。
    const port: CatalogOutboxPersistencePort = {
      load: async () => [{
        partition_key: "wrong-key",
        client_sequence: 1,
        operation: outbox.operations[0],
      }],
      write: async () => undefined,
      delete: async () => undefined,
    };
    await expect(loadCatalogOutbox(port, PARTITION))
      .rejects.toThrow("outbox 记录的分区键与请求不符");
  });

  test("记录主键与 operation 序号不一致时必须响亮失败", async () => {
    const outbox = outboxWithRename();
    const port: CatalogOutboxPersistencePort = {
      load: async () => [{
        partition_key: "local:8014\u0000workspace-1\u0000guest",
        client_sequence: 9,
        operation: outbox.operations[0],
      }],
      write: async () => undefined,
      delete: async () => undefined,
    };
    await expect(loadCatalogOutbox(port, PARTITION))
      .rejects.toThrow("outbox 记录主键与 operation 序号不一致");
  });
});

describe("会话目录 outbox 持久层：写入失败必须透明抛出", () => {
  test("本地存储写入失败时原样抛出，绝不静默降级为内存态", async () => {
    const failing: CatalogOutboxPersistencePort = {
      load: async () => [],
      write: async () => {
        throw new Error("QuotaExceededError: 本地存储已满");
      },
      delete: async () => undefined,
    };
    await expect(persistCatalogOutboxPendingOperations(
      failing,
      PARTITION,
      outboxWithRename().operations,
    )).rejects.toThrow("QuotaExceededError: 本地存储已满");
  });
});

describe("会话目录 outbox 真 IndexedDB 持久层", () => {
  test("真实 IndexedDB 往返保留操作状态与序号", async () => {
    const database = await openCatalogOutboxDatabase();
    const port = createIndexedDbCatalogOutboxPort(database);
    const persisted = markCatalogOutboxOperationPersisted(outboxWithRename(), opId("a"));
    await persistCatalogOutboxPendingOperations(port, PARTITION, persisted.operations);
    const restored = await loadCatalogOutbox(port, PARTITION);
    expect(restored.operations.map((item) => item.client_operation_id)).toEqual([opId("a")]);
    expect(restored.operations[0].state).toBe("persisted");
    expect(restored.next_client_sequence).toBe(2);
  });

  test("真实 IndexedDB 删除后不再恢复，且不影响其它分区", async () => {
    const database = await openCatalogOutboxDatabase();
    const port = createIndexedDbCatalogOutboxPort(database);
    await persistCatalogOutboxPendingOperations(port, PARTITION, outboxWithRename().operations);
    await persistCatalogOutboxPendingOperations(port, OTHER_PARTITION, outboxWithRename().operations);
    await deleteCatalogOutboxOperations(port, PARTITION, [1]);
    expect((await loadCatalogOutbox(port, PARTITION)).operations).toEqual([]);
    expect((await loadCatalogOutbox(port, OTHER_PARTITION)).operations.length).toBe(1);
  });

  test("环境没有 IndexedDB 时打开数据库必须响亮失败", () => {
    Reflect.deleteProperty(globalThis, "indexedDB");
    try {
      expect(() => openCatalogOutboxDatabase())
        .toThrow("当前运行环境没有 IndexedDB");
    } finally {
      installIndexedDbFake();
    }
  });
});

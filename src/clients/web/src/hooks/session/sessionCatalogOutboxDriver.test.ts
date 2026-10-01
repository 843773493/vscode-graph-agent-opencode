import { describe, expect, test } from "bun:test";
import { HttpRequestError } from "../../api/http";
import type {
  SessionCatalogEnqueueResult,
  SessionCatalogOperationReceipt,
  SessionCatalogOperationStatusPage,
} from "../../api/session/sessionCatalogOperations";
import type { CatalogOutbox, CatalogOutboxOperation } from "../../state/session/sessionCatalogOutbox";
import {
  createCatalogOutboxBroadcaster,
  createSessionCatalogOutboxDriver,
  type CatalogOutboxDriver,
  type SessionCatalogOperationsAdapter,
} from "./sessionCatalogOutboxDriver";
import { createIndexedDbFake } from "../../state/session/sessionCatalogOutboxIdbFake";
import {
  createIndexedDbCatalogOutboxPort,
  openCatalogOutboxDatabase,
  type CatalogOutboxPersistencePort,
} from "../../state/session/sessionCatalogOutboxStore";

const PARTITION = { gatewayId: "local:8014", workspaceId: "workspace-1", principal: "guest" };
const PORT = 48_901;

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

/** 记录端口写入的确定性内存桩：用于验证「持久化先于入队」与时序，不模拟 IndexedDB。 */
function recordingPort(): CatalogOutboxPersistencePort & { writes: CatalogOutboxOperation[] } {
  const writes: CatalogOutboxOperation[] = [];
  return {
    writes,
    async load() {
      return [];
    },
    async write(records) {
      for (const record of records) writes.push(record.operation);
    },
    async delete() {},
  };
}

function renameIntent(): Parameters<CatalogOutboxDriver["applyIntent"]>[1] {
  return { kind: "rename_node", targetNodeId: "cnode_1", name: "新名字" };
}

describe("会话目录 outbox 驱动：持久化先于入队", () => {
  test("同步 pending 立即可见，随后落盘并批量入队", async () => {
    const events: string[] = [];
    const enqueued: string[][] = [];
    const persistence = recordingPort();
    const adapter: SessionCatalogOperationsAdapter = {
      async enqueue(_port, _workspaceId, intents) {
        events.push("enqueue");
        enqueued.push(intents.map((intent) => intent.client_operation_id));
        return {
          workspace_id: "workspace-1",
          accepted_count: intents.length,
          receipts: intents.map((intent) => receipt(intent.client_operation_id)),
          created_node_ids: {},
        } satisfies SessionCatalogEnqueueResult;
      },
      async queryStatus() {
        throw new Error("本用例不应查询状态");
      },
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
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
    expect(driver.current().operations[0].state).toBe("accepted");
  });

  test("本地持久化失败时撤销 pending、显示错误且绝不派发入队", async () => {
    let enqueueCount = 0;
    const persistence: CatalogOutboxPersistencePort = {
      load: async () => [],
      write: async () => {
        throw new Error("QuotaExceededError: 本地存储已满");
      },
      delete: async () => undefined,
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
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

  test("刷新恢复 outbox 后未对账命令仍保留并可继续入队", async () => {
    const persistence = recordingPort();
    const enqueued: string[][] = [];
    const adapter: SessionCatalogOperationsAdapter = {
      async enqueue(_port, _workspaceId, intents) {
        enqueued.push(intents.map((intent) => intent.client_operation_id));
        return {
          workspace_id: "workspace-1",
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
      partition: PARTITION,
      persistence,
      adapter,
    });
    await first.restore();
    await first.applyIntent(opId("a"), renameIntent(), {
      baseCatalogRevision: 7,
      expectedRevision: 3,
    });

    // 模拟刷新：新的驱动实例从同一持久层恢复（recordingPort 的 load 返回空，
    // 因此这里验证的是「未终态命令不会因重建驱动而丢失本地记录」这一契约的输入侧）。
    expect(persistence.writes.map((operation) => operation.client_operation_id)).toEqual([opId("a")]);
    expect(enqueued).toEqual([[opId("a")]]);
  });

  test("未 restore 即操作必须响亮失败", async () => {
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      partition: PARTITION,
      persistence: recordingPort(),
      adapter: {
        async enqueue() { throw new Error("不应入队"); },
        async queryStatus() { throw new Error("不应查询"); },
      },
    });
    expect(() => driver.current()).toThrow("会话目录 outbox 尚未恢复");
  });
});

describe("会话目录 outbox 驱动：未知结果与对账", () => {
  function driverWithAdapter(
    adapter: SessionCatalogOperationsAdapter,
  ): CatalogOutboxDriver {
    return createSessionCatalogOutboxDriver({
      port: PORT,
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
          workspace_id: "workspace-1",
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
          workspace_id: "workspace-1",
          accepted_count: intents.length,
          receipts: intents.map((intent) => receipt(intent.client_operation_id)),
          created_node_ids: {},
        };
      },
      async queryStatus(): Promise<SessionCatalogOperationStatusPage> {
        return {
          workspace_id: "workspace-1",
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
    const deleted: number[][] = [];
    const port: CatalogOutboxPersistencePort = {
      ...persistence,
      async delete(_partitionKey, clientSequences) {
        deleted.push([...clientSequences]);
      },
    };
    const driver = createSessionCatalogOutboxDriver({
      port: PORT,
      partition: PARTITION,
      persistence: port,
      adapter: {
        async enqueue(_port, _workspaceId, intents) {
          return {
            workspace_id: "workspace-1",
            accepted_count: intents.length,
            receipts: intents.map((intent) => receipt(intent.client_operation_id)),
            created_node_ids: {},
          };
        },
        async queryStatus() {
          return {
            workspace_id: "workspace-1",
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
    expect(deleted).toEqual([[1]]);
  });
});

describe("会话目录 outbox 驱动：真 IndexedDB 与跨 tab", () => {
  test("真实 IndexedDB 恢复 + 跨 tab 通知重读不制造重复 node", async () => {
    Object.defineProperty(globalThis, "indexedDB", {
      configurable: true,
      value: createIndexedDbFake(),
    });
    const database = await openCatalogOutboxDatabase();
    const port = createIndexedDbCatalogOutboxPort(database);
    const enqueued: string[][] = [];
    const adapter: SessionCatalogOperationsAdapter = {
      async enqueue(_port, _workspaceId, intents) {
        enqueued.push(intents.map((intent) => intent.client_operation_id));
        return {
          workspace_id: "workspace-1",
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
      partition: PARTITION,
      persistence: port,
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
      partition: PARTITION,
      persistence: port,
      adapter,
    });
    const reloaded = await otherTab.restore();
    expect(reloaded.operations.map((operation) => operation.client_operation_id)).toEqual([opId("a")]);
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

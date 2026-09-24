import { describe, expect, test } from "bun:test";
import {
  addCatalogOutboxIntent,
  applyCatalogOutboxReceipts,
  catalogOutboxBatchToIntents,
  catalogOutboxOperationIsTerminal,
  catalogOutboxPartitionKey,
  createCatalogOutbox,
  markCatalogOutboxOperationPersisted,
  markCatalogOutboxOperationsUnknown,
  orderedCatalogOutboxOperations,
  planCatalogOutboxBatch,
  pruneReconciledCatalogOutbox,
  resolveCatalogOutboxFailures,
  rollbackCatalogOutboxIntents,
  unsettledCatalogOutboxOperations,
  type CatalogOutbox,
  type CatalogOutboxOperation,
} from "./sessionCatalogOutbox";
import type { SessionCatalogOperationReceipt } from "../../api/session/sessionCatalogOperations";

const LOCAL_PARTITION = {
  gatewayId: "local:8014",
  workspaceId: "workspace-1",
  principal: "guest",
};

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

function outboxWithRename(): CatalogOutbox {
  return addCatalogOutboxIntent(
    createCatalogOutbox(LOCAL_PARTITION),
    opId("a"),
    { kind: "rename_node", targetNodeId: "node-1", name: "新名字" },
    { baseCatalogRevision: 7, expectedRevision: 3 },
  );
}

function operation(outbox: CatalogOutbox, operationId: string): CatalogOutboxOperation {
  const found = outbox.operations.find(
    (item) => item.client_operation_id === operationId,
  );
  if (!found) throw new Error(`测试缺少 operation: ${operationId}`);
  return found;
}

describe("会话目录 outbox 分区键", () => {
  test("按 gateway/workspace/principal 三元组隔离，不同分区绝不互相串键", () => {
    const base = catalogOutboxPartitionKey(LOCAL_PARTITION);
    expect(base).toBe("local:8014\u0000workspace-1\u0000guest");
    expect(catalogOutboxPartitionKey({ ...LOCAL_PARTITION, workspaceId: "workspace-2" }))
      .not.toBe(base);
    expect(catalogOutboxPartitionKey({ ...LOCAL_PARTITION, principal: "user_a" }))
      .not.toBe(base);
    expect(catalogOutboxPartitionKey({ ...LOCAL_PARTITION, gatewayId: "gw_remote" }))
      .not.toBe(base);
  });

  test("任一分量为空时必须响亮失败而不是拼出会碰撞的键", () => {
    expect(() => catalogOutboxPartitionKey({ ...LOCAL_PARTITION, principal: "" }))
      .toThrow("outbox 分区键的 principal 必须是非空字符串");
    expect(() => createCatalogOutbox({ ...LOCAL_PARTITION, gatewayId: "" }))
      .toThrow("outbox 分区键的 gatewayId 必须是非空字符串");
  });
});

describe("会话目录 outbox 意图登记", () => {
  test("同步登记 pending_local 并给出单调 client_sequence", () => {
    let outbox = outboxWithRename();
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("b"),
      { kind: "move_node", targetNodeId: "node-2", parentNodeId: "node-1" },
      { baseCatalogRevision: 7, expectedRevision: 4 },
    );
    const ordered = orderedCatalogOutboxOperations(outbox);
    expect(ordered.map((item) => item.client_operation_id)).toEqual([opId("a"), opId("b")]);
    expect(ordered.map((item) => item.client_sequence)).toEqual([1, 2]);
    expect(ordered.map((item) => item.state)).toEqual(["pending_local", "pending_local"]);
    expect(outbox.next_client_sequence).toBe(3);
  });

  test("create_folder 用 client_ref 投影且不接受 target_node_id", () => {
    const outbox = addCatalogOutboxIntent(
      createCatalogOutbox(LOCAL_PARTITION),
      opId("folder"),
      { kind: "create_folder", name: "新文件夹", parentNodeId: null },
      { baseCatalogRevision: 0 },
    );
    const created = operation(outbox, opId("folder"));
    expect(created.client_ref).toBe(opId("folder"));
    expect(created.created_node_id).toBeNull();
    expect(created.target_node_id).toBeNull();
    expect(() => addCatalogOutboxIntent(
      createCatalogOutbox(LOCAL_PARTITION),
      opId("bad"),
      { kind: "create_folder", name: "坏", targetNodeId: "node-1" },
      { baseCatalogRevision: 0 },
    )).toThrow("create_folder 不接受 target_node_id");
  });

  test("悬空依赖与自依赖必须在本地响亮失败", () => {
    expect(() => addCatalogOutboxIntent(
      createCatalogOutbox(LOCAL_PARTITION),
      opId("x"),
      { kind: "rename_node", targetNodeId: "node-1", name: "x" },
      { baseCatalogRevision: 1, expectedRevision: 1, dependsOn: [opId("missing")] },
    )).toThrow("depends_on 不在本 outbox 内");
    expect(() => addCatalogOutboxIntent(
      createCatalogOutbox(LOCAL_PARTITION),
      opId("x"),
      { kind: "rename_node", targetNodeId: "node-1", name: "x" },
      { baseCatalogRevision: 1, expectedRevision: 1, dependsOn: [opId("x")] },
    )).toThrow("depends_on 不能引用自身");
  });

  test("rename/move 缺少 expected_revision 时必须响亮失败", () => {
    expect(() => addCatalogOutboxIntent(
      createCatalogOutbox(LOCAL_PARTITION),
      opId("m"),
      { kind: "move_node", targetNodeId: "node-1", parentNodeId: null },
      { baseCatalogRevision: 1 },
    )).toThrow("move_node 必须给出目标 node 的 expected_revision");
  });

  test("同 ID 重复登记必须响亮失败", () => {
    expect(() => addCatalogOutboxIntent(
      outboxWithRename(),
      opId("a"),
      { kind: "rename_node", targetNodeId: "node-1", name: "又改" },
      { baseCatalogRevision: 7, expectedRevision: 3 },
    )).toThrow("outbox 已存在同 ID operation");
  });
});

describe("会话目录 outbox 持久化推进与回退", () => {
  test("只有 persisted 之后才进入入队批次", () => {
    const pending = outboxWithRename();
    expect(planCatalogOutboxBatch(pending, { maxBatchSize: 10 })).toEqual([]);
    const persisted = markCatalogOutboxOperationPersisted(pending, opId("a"));
    expect(planCatalogOutboxBatch(persisted, { maxBatchSize: 10 })
      .map((item) => item.client_operation_id)).toEqual([opId("a")]);
  });

  test("持久化失败撤销该意图及其传递依赖", () => {
    let outbox = addCatalogOutboxIntent(
      createCatalogOutbox(LOCAL_PARTITION),
      opId("folder"),
      { kind: "create_folder", name: "新文件夹", parentNodeId: null },
      { baseCatalogRevision: 0 },
    );
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("move"),
      { kind: "move_node", targetNodeId: "ses_1", parentCreatedByOperationId: opId("folder") },
      { baseCatalogRevision: 0, expectedRevision: 2 },
    );
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("unrelated"),
      { kind: "rename_node", targetNodeId: "node-9", name: "无关改名" },
      { baseCatalogRevision: 0, expectedRevision: 1 },
    );

    const rollback = rollbackCatalogOutboxIntents(outbox, [opId("folder")]);
    expect(rollback.removed_operation_ids).toEqual([opId("folder"), opId("move")]);
    expect(rollback.outbox.operations.map((item) => item.client_operation_id))
      .toEqual([opId("unrelated")]);
  });

  test("删掉父节点时依赖它的 pending 移动必须连带撤销", () => {
    let outbox = addCatalogOutboxIntent(
      createCatalogOutbox(LOCAL_PARTITION),
      opId("folder"),
      { kind: "create_folder", name: "F", parentNodeId: null },
      { baseCatalogRevision: 0 },
    );
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("move"),
      { kind: "move_node", targetNodeId: "ses_1", parentNodeId: "node-folder" },
      { baseCatalogRevision: 0, expectedRevision: 2, dependsOn: [opId("folder")] },
    );
    const rollback = rollbackCatalogOutboxIntents(outbox, [opId("folder")]);
    expect(rollback.outbox.operations).toEqual([]);
  });
});

describe("会话目录 outbox 有序批量入队", () => {
  test("同 node 连续编辑按 client_sequence 顺序且依赖可在同批解析", () => {
    let outbox = outboxWithRename();
    outbox = markCatalogOutboxOperationPersisted(outbox, opId("a"));
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("b"),
      { kind: "move_node", targetNodeId: "node-1", parentNodeId: null },
      { baseCatalogRevision: 7, expectedRevision: 4, dependsOn: [opId("a")] },
    );
    outbox = markCatalogOutboxOperationPersisted(outbox, opId("b"));

    const batch = planCatalogOutboxBatch(outbox, { maxBatchSize: 10 });
    expect(batch.map((item) => item.client_operation_id)).toEqual([opId("a"), opId("b")]);
    expect(catalogOutboxBatchToIntents(batch)).toEqual([
      {
        client_operation_id: opId("a"),
        client_sequence: 1,
        kind: "rename_node",
        base_catalog_revision: 7,
        expected_revision: 3,
        target_node_id: "node-1",
        name: "新名字",
        depends_on: [],
      },
      {
        client_operation_id: opId("b"),
        client_sequence: 2,
        kind: "move_node",
        base_catalog_revision: 7,
        expected_revision: 4,
        target_node_id: "node-1",
        parent_node_id: null,
        depends_on: [opId("a")],
      },
    ]);
  });

  test("依赖未取得 acceptance 时后继不得越序入队", () => {
    let outbox = outboxWithRename();
    outbox = markCatalogOutboxOperationPersisted(outbox, opId("a"));
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("b"),
      { kind: "rename_node", targetNodeId: "node-1", name: "第二次改名" },
      { baseCatalogRevision: 7, expectedRevision: 3, dependsOn: [opId("a")] },
    );
    outbox = markCatalogOutboxOperationPersisted(outbox, opId("b"));

    // 前一批只含 a：b 依赖 a，但同批已解析，因此两题一起入队；先只让 a 入队来验证越序防护。
    const firstBatch = planCatalogOutboxBatch(outbox, { maxBatchSize: 1 });
    expect(firstBatch.map((item) => item.client_operation_id)).toEqual([opId("a")]);
    // b 还没进批次：把它单独留在 persisted 且 a 尚未 accepted 时，b 必须继续等待。
    expect(planCatalogOutboxBatch(outbox, { maxBatchSize: 1 })
      .some((item) => item.client_operation_id === opId("b"))).toBe(false);
  });

  test("依赖 accepted 后才放行后继", () => {
    let outbox = outboxWithRename();
    outbox = markCatalogOutboxOperationPersisted(outbox, opId("a"));
    outbox = applyCatalogOutboxReceipts(outbox, [receipt(opId("a"))]);
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("b"),
      { kind: "rename_node", targetNodeId: "node-1", name: "第二次改名" },
      { baseCatalogRevision: 7, expectedRevision: 3, dependsOn: [opId("a")] },
    );
    outbox = markCatalogOutboxOperationPersisted(outbox, opId("b"));
    expect(planCatalogOutboxBatch(outbox, { maxBatchSize: 10 })
      .map((item) => item.client_operation_id)).toEqual([opId("b")]);
  });

  test("批次上限必须是正整数", () => {
    expect(() => planCatalogOutboxBatch(outboxWithRename(), { maxBatchSize: 0 }))
      .toThrow("入队批次上限必须是正整数");
  });

  test("unknown 未确认时不得入队，服务端确认不认识该 ID 后才按同一 ID 重试", () => {
    let outbox = markCatalogOutboxOperationPersisted(outboxWithRename(), opId("a"));
    outbox = markCatalogOutboxOperationsUnknown(outbox, [opId("a")]);
    expect(planCatalogOutboxBatch(outbox, { maxBatchSize: 10 })).toEqual([]);
    const retried = planCatalogOutboxBatch(outbox, {
      maxBatchSize: 10,
      retryableOperationIds: [opId("a")],
    });
    expect(retried.map((item) => item.client_operation_id)).toEqual([opId("a")]);
    expect(retried[0].client_operation_id).toBe(opId("a"));
  });
});

describe("会话目录 outbox 对账", () => {
  test("202 receipt 把 client_ref 解析为 canonical ID 并清空本地引用", () => {
    let outbox = addCatalogOutboxIntent(
      createCatalogOutbox(LOCAL_PARTITION),
      opId("folder"),
      { kind: "create_folder", name: "新文件夹", parentNodeId: null },
      { baseCatalogRevision: 0 },
    );
    outbox = applyCatalogOutboxReceipts(outbox, [receipt(opId("folder"), {
      kind: "create_folder",
      state: "queued",
      created_node_id: "cnode_real",
      queue_seq: 9,
      receipt_revision: 12,
    })]);
    const created = operation(outbox, opId("folder"));
    // 202 只说明 durable acceptance：本地推进为 accepted，服务端状态另存 server_state。
    expect(created.state).toBe("accepted");
    expect(created.server_state).toBe("queued");
    expect(created.created_node_id).toBe("cnode_real");
    expect(created.client_ref).toBeNull();
    expect(created.queue_seq).toBe(9);
    expect(created.receipt_revision).toBe(12);
  });

  test("未登记 operation 的 receipt 必须响亮失败", () => {
    expect(() => applyCatalogOutboxReceipts(outboxWithRename(), [receipt(opId("ghost"))]))
      .toThrow("收到未登记 operation 的 receipt");
  });

  test("终态 operation 的迟到 receipt 不得回退已对账状态", () => {
    let outbox = outboxWithRename();
    outbox = applyCatalogOutboxReceipts(outbox, [receipt(opId("a"), { state: "committed" })]);
    outbox = applyCatalogOutboxReceipts(outbox, [receipt(opId("a"), { state: "running" })]);
    expect(operation(outbox, opId("a")).state).toBe("committed");
    expect(catalogOutboxOperationIsTerminal(operation(outbox, opId("a")))).toBe(true);
  });

  test("明确拒绝移除失败及传递依赖，保留独立待重放命令", () => {
    let outbox = addCatalogOutboxIntent(
      createCatalogOutbox(LOCAL_PARTITION),
      opId("folder"),
      { kind: "create_folder", name: "F", parentNodeId: null },
      { baseCatalogRevision: 0 },
    );
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("move"),
      { kind: "move_node", targetNodeId: "ses_1", parentCreatedByOperationId: opId("folder") },
      { baseCatalogRevision: 0, expectedRevision: 3 },
    );
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("independent"),
      { kind: "rename_node", targetNodeId: "node-other", name: "独立改名" },
      { baseCatalogRevision: 0, expectedRevision: 1 },
    );
    outbox = applyCatalogOutboxReceipts(outbox, [
      receipt(opId("folder"), { kind: "create_folder", state: "rejected", error_code: "name_conflict" }),
    ]);

    const resolution = resolveCatalogOutboxFailures(outbox);
    expect(resolution.failed_operation_ids).toEqual([opId("folder")]);
    expect(resolution.removed_operation_ids).toEqual([opId("folder"), opId("move")]);
    expect(resolution.outbox.operations.map((item) => item.client_operation_id))
      .toEqual([opId("independent")]);
  });

  test("committed 事实不得被后续回退撤销", () => {
    let outbox = outboxWithRename();
    outbox = applyCatalogOutboxReceipts(outbox, [receipt(opId("a"), { state: "committed" })]);
    outbox = applyCatalogOutboxReceipts(outbox, [receipt(opId("a"), { state: "rejected" })]);
    const failed = resolveCatalogOutboxFailures(outbox);
    expect(failed.removed_operation_ids).toEqual([]);
    expect(failed.outbox.operations.map((item) => item.client_operation_id)).toEqual([opId("a")]);
  });

  test("只清理已确认对账 revision 的终态条目", () => {
    let outbox = outboxWithRename();
    outbox = applyCatalogOutboxReceipts(outbox, [
      receipt(opId("a"), { state: "committed", receipt_revision: 20 }),
    ]);
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("b"),
      { kind: "rename_node", targetNodeId: "node-1", name: "第二次" },
      { baseCatalogRevision: 7, expectedRevision: 3 },
    );
    const pruned = pruneReconciledCatalogOutbox(outbox, 19);
    expect(pruned.operations.map((item) => item.client_operation_id)).toEqual([opId("a"), opId("b")]);
    const finalized = pruneReconciledCatalogOutbox(outbox, 20);
    expect(finalized.operations.map((item) => item.client_operation_id)).toEqual([opId("b")]);
  });

  test("未终态命令绝不进入清理范围", () => {
    const outbox = addCatalogOutboxIntent(
      createCatalogOutbox(LOCAL_PARTITION),
      opId("a"),
      { kind: "rename_node", targetNodeId: "node-1", name: "新名字" },
      { baseCatalogRevision: 7, expectedRevision: 3 },
    );
    expect(pruneReconciledCatalogOutbox(outbox, 1_000_000).operations
      .map((item) => item.client_operation_id)).toEqual([opId("a")]);
  });

  test("unknown 属于未终态：仍留在投影与对账集合中", () => {
    let outbox = markCatalogOutboxOperationPersisted(outboxWithRename(), opId("a"));
    outbox = markCatalogOutboxOperationsUnknown(outbox, [opId("a")]);
    expect(unsettledCatalogOutboxOperations(outbox)
      .map((item) => item.client_operation_id)).toEqual([opId("a")]);
    expect(catalogOutboxOperationIsTerminal(operation(outbox, opId("a")))).toBe(false);
    expect(pruneReconciledCatalogOutbox(outbox, 1_000_000).operations
      .map((item) => item.client_operation_id)).toEqual([opId("a")]);
  });

  test("unknown 不得覆盖已终态 operation", () => {
    let outbox = outboxWithRename();
    outbox = applyCatalogOutboxReceipts(outbox, [receipt(opId("a"), { state: "committed" })]);
    outbox = markCatalogOutboxOperationsUnknown(outbox, [opId("a")]);
    expect(operation(outbox, opId("a")).state).toBe("committed");
  });
});

import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { TestRunContext } from "../../../harness/js/run-context.mjs";

const projectRoot = process.cwd();
const runContext = TestRunContext.fromTestFile(fileURLToPath(import.meta.url), projectRoot);
const artifactDirectory = runContext.artifactsDir;
const runtimeDirectory = path.join(runContext.outputRoot, "runtime");
await mkdir(runtimeDirectory, { recursive: true });
for (const name of ["TMPDIR", "TMP", "TEMP"]) process.env[name] = runtimeDirectory;
const bundleDirectory = path.join(artifactDirectory, "bundle");
await mkdir(bundleDirectory, { recursive: true });

const build = await Bun.build({
  entrypoints: [
    path.join(projectRoot, "src/clients/web/src/state/session/sessionCatalogOutbox.ts"),
    path.join(projectRoot, "src/clients/web/src/state/session/sessionCatalogOutboxStore.ts"),
    path.join(projectRoot, "src/clients/web/src/state/session/sessionCatalogProjection.ts"),
  ],
  target: "browser",
  outdir: bundleDirectory,
  naming: "[name].js",
  sourcemap: "none",
});
if (!build.success) {
  throw new AggregateError(build.logs, "打包会话目录 outbox 浏览器 harness 失败");
}

const bundlePaths = new Map(
  build.outputs.map((output) => [`/${path.basename(output.path)}`, output.path]),
);
for (const moduleName of ["sessionCatalogOutbox.js", "sessionCatalogOutboxStore.js", "sessionCatalogProjection.js"]) {
  if (!bundlePaths.has(`/${moduleName}`)) {
    throw new Error(`浏览器 harness 缺少生产模块: ${moduleName}`);
  }
}

const html = `<!doctype html>
<html lang="zh-CN">
  <head><meta charset="utf-8"><title>Session catalog outbox IndexedDB harness</title></head>
  <body>
    <script type="module">
      import * as state from "/sessionCatalogOutbox.js";
      import * as storage from "/sessionCatalogOutboxStore.js";
      import * as projection from "/sessionCatalogProjection.js";
      window.outboxModules = { state, storage, projection };
    </script>
  </body>
</html>`;

let executionError;
try {
  const server = createServer(async (request, response) => {
    const pathname = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
    if (pathname === "/") {
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      response.end(html);
      return;
    }
    const bundlePath = bundlePaths.get(pathname);
    if (bundlePath) {
      response.writeHead(200, { "Content-Type": "text/javascript; charset=utf-8" });
      response.end(await readFile(bundlePath));
      return;
    }
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    response.end(`未找到 harness 资源: ${pathname}`);
  });

  runContext.addCleanup("outbox HTTP server", async () => {
    if (server.listening) {
      await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });

  const address = server.address();
  if (!address || typeof address === "string") throw new Error("测试 harness 未取得 TCP 端口");
  const baseUrl = `http://127.0.0.1:${address.port}`;

  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  function equal(actual, expected, message) {
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
      throw new Error(`${message}: 实际 ${JSON.stringify(actual)}，期望 ${JSON.stringify(expected)}`);
    }
  }

  const partition = {
    gatewayId: "idb-browser-test",
    workspaceId: `workspace-${randomUUID()}`,
    principal: "guest",
  };
  const upgradePartition = { ...partition, workspaceId: `upgrade-${randomUUID()}` };
  const collisionPartition = { ...partition, workspaceId: `collision-${randomUUID()}` };
  const receiptPartition = { ...partition, workspaceId: `receipt-${randomUUID()}` };
  const operationIds = {
    upgrade: randomUUID().replaceAll("-", ""),
    first: randomUUID().replaceAll("-", ""),
    second: randomUUID().replaceAll("-", ""),
    afterPrune: randomUUID().replaceAll("-", ""),
    sequenceOriginal: randomUUID().replaceAll("-", ""),
    sequenceCollision: randomUUID().replaceAll("-", ""),
    durableReceipt: randomUUID().replaceAll("-", ""),
  };

  async function insertFromPage(page, targetPartition, operationId, name) {
    return await page.evaluate(async ({ targetPartition: partitionValue, operationId: id, name: operationName }) => {
      const { state, storage } = window.outboxModules;
      const database = window.outboxDatabase ?? await storage.openCatalogOutboxDatabase();
      window.outboxDatabase = database;
      const port = window.outboxPort ?? storage.createIndexedDbCatalogOutboxPort(database);
      window.outboxPort = port;
      const operation = state.addCatalogOutboxIntent(
        state.createCatalogOutbox(partitionValue),
        id,
        { kind: "rename_node", targetNodeId: "node-1", name: operationName },
        { baseCatalogRevision: 7, expectedRevision: 3 },
      ).operations[0];
      return await storage.persistCatalogOutboxPendingOperation(port, partitionValue, operation);
    }, { targetPartition, operationId, name });
  }

  async function readPartition(page, targetPartition) {
    return await page.evaluate(async (partitionValue) => {
      const { storage } = window.outboxModules;
      const database = window.outboxDatabase ?? await storage.openCatalogOutboxDatabase();
      window.outboxDatabase = database;
      const port = window.outboxPort ?? storage.createIndexedDbCatalogOutboxPort(database);
      window.outboxPort = port;
      const partitionKey = window.outboxModules.state.catalogOutboxPartitionKey(partitionValue);
      const records = await port.load(partitionKey);
      return records.map((record) => record);
    }, targetPartition);
  }

  async function applyReceiptsFromPage(page, targetPartition, receipts) {
    return await page.evaluate(async ({ targetPartition: partitionValue, receipts: items }) => {
      const { state, storage } = window.outboxModules;
      const database = window.outboxDatabase ?? await storage.openCatalogOutboxDatabase();
      window.outboxDatabase = database;
      const port = window.outboxPort ?? storage.createIndexedDbCatalogOutboxPort(database);
      window.outboxPort = port;
      return await port.applyReceipts(state.catalogOutboxPartitionKey(partitionValue), items);
    }, { targetPartition, receipts });
  }

  let outcome;
  let browser;
  let context;
  let pageA;
  let pageB;
  const browserErrors = [];
  browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined,
    headless: true,
  });
  runContext.addCleanup("outbox Chromium", () => browser.close());
  context = await browser.newContext();
  runContext.addCleanup("outbox browser context", () => context.close());
  pageA = await context.newPage();
  pageB = await context.newPage();
  for (const page of [pageA, pageB]) {
    page.on("pageerror", (error) => browserErrors.push(error.message));
    await page.goto(baseUrl, { waitUntil: "load", timeout: 30_000 });
    await page.waitForFunction(() => Boolean(window.outboxModules), undefined, { timeout: 30_000 });
  }

  const upgrade = await pageA.evaluate(async ({ partitionValue, operationId }) => {
    const { state, storage } = window.outboxModules;
    const operation = state.addCatalogOutboxIntent(
      state.createCatalogOutbox(partitionValue),
      operationId,
      { kind: "rename_node", targetNodeId: "node-legacy", name: "v1 遗留命令" },
      { baseCatalogRevision: 3, expectedRevision: 2 },
    ).operations[0];
    const request = indexedDB.open(storage.CATALOG_OUTBOX_DATABASE_NAME, 1);
    request.onupgradeneeded = () => {
      const legacyOperations = request.result.createObjectStore(storage.CATALOG_OUTBOX_STORE_NAME, {
        keyPath: ["partition_key", "client_sequence"],
      });
      legacyOperations.createIndex("partition_key", "partition_key", { unique: false });
    };
    const legacyDatabase = await new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error ?? new Error("创建 v1 outbox 失败"));
    });
    const transaction = legacyDatabase.transaction(storage.CATALOG_OUTBOX_STORE_NAME, "readwrite");
    transaction.objectStore(storage.CATALOG_OUTBOX_STORE_NAME).add({
      partition_key: state.catalogOutboxPartitionKey(partitionValue),
      client_sequence: 1,
      operation: { ...operation, client_sequence: 1 },
    });
    await new Promise((resolve, reject) => {
      transaction.oncomplete = resolve;
      transaction.onabort = () => reject(transaction.error ?? new Error("写入 v1 outbox 失败"));
      transaction.onerror = () => reject(transaction.error ?? new Error("写入 v1 outbox 失败"));
    });
    legacyDatabase.close();

    const database = await storage.openCatalogOutboxDatabase();
    window.outboxDatabase = database;
    const port = storage.createIndexedDbCatalogOutboxPort(database);
    window.outboxPort = port;
    const restored = await storage.loadCatalogOutbox(port, partitionValue);
    const next = state.addCatalogOutboxIntent(
      restored,
      `${operationId}-next`,
      { kind: "rename_node", targetNodeId: "node-legacy", name: "v2 新命令" },
      { baseCatalogRevision: 3, expectedRevision: 2 },
    ).operations[0];
    const inserted = await storage.persistCatalogOutboxPendingOperation(port, partitionValue, next);
    return {
      restored: restored.operations.map((item) => ({
        client_operation_id: item.client_operation_id,
        state: item.state,
        name: item.name,
      })),
      next_sequence: inserted.client_sequence,
    };
  }, { partitionValue: upgradePartition, operationId: operationIds.upgrade });
  equal(
    upgrade.restored,
    [],
    "未发布的 v1 开发期 outbox 数据必须在升级时整体删除",
  );
  assert(upgrade.next_sequence === 1, "清空未发布的 v1 outbox 后新序号必须从 1 开始");

  const [first, second] = await Promise.all([
    insertFromPage(pageA, partition, operationIds.first, "原始命令 A"),
    insertFromPage(pageB, partition, operationIds.second, "原始命令 B"),
  ]);
  const concurrentSequences = [first.client_sequence, second.client_sequence].sort((a, b) => a - b);
  equal(concurrentSequences, [1, 2], "两个独立页面必须取得不同且递增的序号");

  const firstPagePort = await pageA.evaluate(async (targetPartition) => {
    const port = window.outboxPort;
    const key = window.outboxModules.state.catalogOutboxPartitionKey(targetPartition);
    const records = await port.load(key);
    return records.map((record) => ({
      client_sequence: record.client_sequence,
      client_operation_id: record.operation.client_operation_id,
    }));
  }, partition);
  equal(firstPagePort.map((record) => record.client_sequence), [1, 2], "共享 IDB 连接读取并发写入");

  const prunedSequence = Math.max(first.client_sequence, second.client_sequence);
  const prunedOperationId = first.client_sequence === prunedSequence
    ? operationIds.first
    : operationIds.second;
  await pageA.evaluate(async ({ partitionValue, operationId }) => {
    const { storage } = window.outboxModules;
    await storage.deleteCatalogOutboxOperations(window.outboxPort, partitionValue, [operationId]);
  }, { partitionValue: partition, operationId: prunedOperationId });
  const afterPrune = await insertFromPage(pageB, partition, operationIds.afterPrune, "剪枝后命令");
  assert(afterPrune.client_sequence > prunedSequence, "剪枝最高序号后高水位必须保持单调");

  await Promise.all([pageA, pageB].map((page) => page.evaluate(() => {
    window.outboxDatabase.close();
    window.outboxDatabase = null;
    window.outboxPort = null;
  })));
  const restored = await pageA.evaluate(async (partitionValue) => {
    const { state, storage } = window.outboxModules;
    const database = await storage.openCatalogOutboxDatabase();
    window.outboxDatabase = database;
    const port = storage.createIndexedDbCatalogOutboxPort(database);
    window.outboxPort = port;
    const outbox = await storage.loadCatalogOutbox(port, partitionValue);
    return {
      operations: outbox.operations.map((operation) => ({
        client_operation_id: operation.client_operation_id,
        client_sequence: operation.client_sequence,
        state: operation.state,
        name: operation.name,
        expected_revision: operation.expected_revision,
      })),
      replayed: state.catalogOutboxBatchToIntents(
        state.planCatalogOutboxBatch(outbox, { maxBatchSize: 10 }),
      ),
    };
  }, partition);
  assert(
    restored.operations.every((operation) => operation.state === "persisted"),
    "重新打开数据库后 pending_local 必须恢复为可重放状态",
  );
  const retainedOperationId = prunedOperationId === operationIds.first
    ? operationIds.second
    : operationIds.first;
  equal(
    restored.operations.map((operation) => operation.client_operation_id).sort(),
    [retainedOperationId, operationIds.afterPrune].sort(),
    "恢复必须保留未剪枝的稳定 operation ID",
  );
  const retainedOriginal = restored.operations.find(
    (operation) => operation.client_operation_id === retainedOperationId,
  );
  const retainedOriginalName = retainedOperationId === operationIds.first
    ? "原始命令 A"
    : "原始命令 B";
  assert(retainedOriginal?.name === retainedOriginalName, "恢复必须保留原始命令 preimage");
  equal(
    restored.replayed.map((intent) => [intent.client_operation_id, intent.name]).sort(),
    [
      [retainedOperationId, retainedOriginalName],
      [operationIds.afterPrune, "剪枝后命令"],
    ].sort(),
    "重放必须使用保存的 operation ID 与原命令数据",
  );

  let duplicateIdRejected = false;
  try {
    await insertFromPage(pageA, partition, retainedOperationId, "冲突命令不得覆盖原命令");
  } catch {
    duplicateIdRejected = true;
  }
  assert(duplicateIdRejected, "同分区重复 operation ID 必须拒绝");
  const afterDuplicate = await readPartition(pageA, partition);
  const originalDuplicateTarget = afterDuplicate.find(
    (record) => record.operation.client_operation_id === retainedOperationId,
  );
  assert(
    originalDuplicateTarget?.operation.name === retainedOriginalName,
    "重复 ID 失败不得改写原记录",
  );

  const sequenceOriginal = await insertFromPage(
    pageA,
    collisionPartition,
    operationIds.sequenceOriginal,
    "主键冲突原记录",
  );
  await pageA.evaluate(async ({ partitionValue, sequenceOriginalValue }) => {
    const { storage } = window.outboxModules;
    const database = window.outboxDatabase;
    const transaction = database.transaction("partition-sequences", "readwrite");
    transaction.objectStore("partition-sequences").put({
      partition_key: [partitionValue.gatewayId, partitionValue.workspaceId, partitionValue.principal]
        .join("\u0000"),
      next_client_sequence: sequenceOriginalValue,
    });
    await new Promise((resolve, reject) => {
      transaction.oncomplete = resolve;
      transaction.onabort = () => reject(transaction.error ?? new Error("高水位测试准备失败"));
      transaction.onerror = () => reject(transaction.error ?? new Error("高水位测试准备失败"));
    });
  }, { partitionValue: collisionPartition, sequenceOriginalValue: sequenceOriginal.client_sequence });

  let sequenceConflictRejected = false;
  try {
    await insertFromPage(
      pageB,
      collisionPartition,
      operationIds.sequenceCollision,
      "序号冲突记录不得覆盖原记录",
    );
  } catch {
    sequenceConflictRejected = true;
  }
  assert(sequenceConflictRejected, "重复主键必须使 operation insert 失败");
  const afterSequenceConflict = await readPartition(pageA, collisionPartition);
  equal(
    afterSequenceConflict.map((record) => ({
      client_sequence: record.client_sequence,
      client_operation_id: record.operation.client_operation_id,
      name: record.operation.name,
    })),
    [{
      client_sequence: sequenceOriginal.client_sequence,
      client_operation_id: operationIds.sequenceOriginal,
      name: "主键冲突原记录",
    }],
    "主键冲突必须回滚高水位并保留原记录",
  );

  const chainPartition = { ...partition, workspaceId: `pending-parent-${randomUUID()}` };
  const chainIds = Array.from({ length: 4 }, () => `op_${randomUUID().replaceAll("-", "")}`);
  const chain = await pageA.evaluate(async ({ partitionValue, ids }) => {
    const { state, storage, projection } = window.outboxModules;
    let outbox = state.createCatalogOutbox(partitionValue);
    for (const [index, name] of ["父目录", "子目录", "孙目录", "独立目录"].entries()) {
      const parent = index === 1 || index === 2
        ? state.catalogOutboxParentCreation(outbox, ids[index - 1]) : null;
      outbox = state.addCatalogOutboxIntent(outbox, ids[index], {
        kind: "create_folder", name, parentNodeId: parent?.parentNodeId ?? null,
        parentCreatedByOperationId: parent?.createdBy,
      }, { baseCatalogRevision: 7, dependsOn: parent ? [parent.createdBy] : [] });
      const operation = outbox.operations.at(-1);
      const inserted = await storage.persistCatalogOutboxPendingOperation(window.outboxPort, partitionValue, operation);
      outbox = state.assignCatalogOutboxOperationSequence(outbox, ids[index], inserted.client_sequence);
      outbox = state.markCatalogOutboxOperationPersisted(outbox, ids[index]);
    }
    const input = { confirmedNodes: [], outbox };
    return {
      child: projection.projectPendingCatalogChildren(ids[0], input).items
        .map((node) => [node.name, node.parent_node_id]),
      grandchild: projection.projectPendingCatalogChildren(ids[1], input).items
        .map((node) => [node.name, node.parent_node_id]),
    };
  }, { partitionValue: chainPartition, ids: chainIds });
  equal(chain.child, [["子目录", chainIds[0]]], "pending 父目录内必须展示原子项");
  equal(chain.grandchild, [["孙目录", chainIds[1]]], "pending 父链不能把孙项错误投影到根");
  await pageA.reload({ waitUntil: "load" });
  await pageA.waitForFunction(() => Boolean(window.outboxModules));
  const recoveredChain = await pageA.evaluate(async ({ partitionValue, ids }) => {
    const { state, storage, projection } = window.outboxModules;
    const database = await storage.openCatalogOutboxDatabase();
    window.outboxDatabase = database;
    window.outboxPort = storage.createIndexedDbCatalogOutboxPort(database);
    const outbox = await storage.loadCatalogOutbox(window.outboxPort, partitionValue);
    const intents = state.catalogOutboxBatchToIntents(state.planCatalogOutboxBatch(outbox, { maxBatchSize: 10 }));
    const failed = state.applyCatalogOutboxReceipts(outbox, outbox.operations.slice(0, 3).map((operation, index) => ({
      operation_id: operation.client_operation_id, client_sequence: operation.client_sequence,
      queue_seq: operation.client_sequence, kind: "create_folder",
      state: index === 0 ? "rejected" : "dependency_failed", created_node_id: null,
      committed_catalog_revision: null, error_code: "parent_creation_failed",
      error_detail: "父目录被明确拒绝", settlement: null,
      receipt_revision: 2, updated_at: "2026-10-08T00:00:00Z",
    })));
    const pruned = state.pruneReconciledCatalogOutbox(failed, 7, ids.slice(0, 3));
    await storage.deleteCatalogOutboxOperations(window.outboxPort, partitionValue, ids.slice(0, 3));
    const root = projection.projectCatalogBranch({
      revision: "fixture-7", catalog_revision: 7, parent_node_id: null,
      items: [], cursor: null, total: 0, consistency_warning: null,
    }, { confirmedNodes: [], outbox: pruned });
    database.close();
    window.outboxDatabase = null;
    window.outboxPort = null;
    return {
      states: outbox.operations.map((operation) => operation.state),
      replay: intents.map((intent) => [intent.client_operation_id, intent.created_by_operation_id, intent.parent_node_id]),
      afterFailureNames: root.items.map((node) => node.name),
      confirmedTotal: root.total,
    };
  }, { partitionValue: chainPartition, ids: chainIds });
  equal(recoveredChain.states, ["persisted", "persisted", "persisted", "persisted"], "刷新重开后父链可按原身份重放");
  equal(recoveredChain.replay, [
    [chainIds[0], null, null], [chainIds[1], chainIds[0], null],
    [chainIds[2], chainIds[1], null], [chainIds[3], null, null],
  ], "刷新重开必须保留同一父创建引用，不将 client_ref 交给后端父 ID");
  equal(recoveredChain.afterFailureNames, ["独立目录"], "父创建失败后终结整个依赖链并保留独立意图");
  assert(recoveredChain.confirmedTotal === 0, "pending 数量不得改写 confirmed total");
  const chainAfterFailure = await readPartition(pageA, chainPartition);
  equal(chainAfterFailure.map((record) => record.operation.client_operation_id), [chainIds[3]], "失败清理后的真实 IDB 重开不能复活依赖链");

  const durableSeed = await pageA.evaluate(async ({ partitionValue, operationId }) => {
    const { state, storage } = window.outboxModules;
    const database = window.outboxDatabase ?? await storage.openCatalogOutboxDatabase();
    window.outboxDatabase = database;
    window.outboxPort ??= storage.createIndexedDbCatalogOutboxPort(database);
    const pending = state.addCatalogOutboxIntent(state.createCatalogOutbox(partitionValue),
      operationId, { kind: "delete_session", targetNodeId: "session_deleted" },
      { baseCatalogRevision: 7 }).operations[0];
    return await storage.persistCatalogOutboxPendingOperation(window.outboxPort, partitionValue, pending);
  }, { partitionValue: receiptPartition, operationId: operationIds.durableReceipt });
  const repairReceipt = {
    operation_id: operationIds.durableReceipt, client_sequence: durableSeed.client_sequence,
    queue_seq: 1, kind: "delete_session", state: "committed", created_node_id: null,
    committed_catalog_revision: 12, settlement: "repair_required", receipt_revision: 3,
    error_code: "cleanup_failed", error_detail: "排空需要修复", updated_at: "2026-10-12T00:00:00Z",
  };
  const abortedReceipt = await pageA.evaluate(async ({ partitionValue, receipt }) => {
    const { state, storage } = window.outboxModules;
    const originalPut = IDBObjectStore.prototype.put;
    let hit = false;
    IDBObjectStore.prototype.put = function (value, ...args) {
      const request = originalPut.call(this, value, ...args);
      if (this.transaction.db.name === storage.CATALOG_OUTBOX_DATABASE_NAME
        && this.name === storage.CATALOG_OUTBOX_STORE_NAME
        && value.operation?.client_operation_id === receipt.operation_id) {
        hit = true;
        queueMicrotask(() => this.transaction.abort());
      }
      return request;
    };
    try {
      await window.outboxPort.applyReceipts(state.catalogOutboxPartitionKey(partitionValue), [receipt]);
      throw new Error("回执事务中止后仍返回成功");
    } catch (error) {
      if (!hit || !String(error).includes("持久合并会话目录回执")) throw error;
      return { hit, error: String(error) };
    } finally {
      IDBObjectStore.prototype.put = originalPut;
    }
  }, { partitionValue: receiptPartition, receipt: repairReceipt });
  const receiptAfterAbort = (await readPartition(pageB, receiptPartition))[0];
  assert(abortedReceipt.hit && receiptAfterAbort.operation.receipt_revision === null,
    "真实回执事务中止必须显式报错且不能写入半条镜像");
  equal(receiptAfterAbort.operation, durableSeed.operation, "中止不改变初始持久命令");
  await applyReceiptsFromPage(pageA, receiptPartition, [repairReceipt]);
  const staleReceipt = { ...repairReceipt, state: "queued", receipt_revision: 2,
    committed_catalog_revision: null, settlement: null, error_code: null, error_detail: null };
  const staleWrite = await applyReceiptsFromPage(pageB, receiptPartition, [staleReceipt]);
  equal(staleWrite[0].operation.receipt_revision, 3, "另一个页面的旧回执不能覆盖较新磁盘版本");
  equal(staleWrite[0].operation.settlement, "repair_required", "旧 enqueue 回执不能清空 repair 事实");
  await Promise.all([pageA, pageB].map((page) => page.evaluate(() => {
    window.outboxDatabase.close();
    window.outboxDatabase = null;
    window.outboxPort = null;
  })));
  const coldRepair = await pageB.evaluate(async (partitionValue) => {
    const { state, storage } = window.outboxModules;
    window.outboxDatabase = await storage.openCatalogOutboxDatabase();
    window.outboxPort = storage.createIndexedDbCatalogOutboxPort(window.outboxDatabase);
    const restored = await storage.loadCatalogOutbox(window.outboxPort, partitionValue);
    const pruned = state.pruneReconciledCatalogOutbox(restored, 12);
    return { restored: restored.operations, retained: pruned.operations };
  }, receiptPartition);
  equal(coldRepair.restored, coldRepair.retained, "冷恢复的 repair_required 不得剪枝");
  equal(coldRepair.restored[0], staleWrite[0].operation, "冷恢复完整消费最新持久回执");
  assert(coldRepair.restored[0].client_sequence === durableSeed.client_sequence,
    "回执持久更新不能重新分配 client_sequence");
  await applyReceiptsFromPage(pageA, receiptPartition, [{ ...repairReceipt,
    receipt_revision: 4, settlement: "settled", error_code: null, error_detail: null }]);
  await pageB.evaluate(async ({ partitionValue, operationId }) => {
    await window.outboxModules.storage.deleteCatalogOutboxOperations(
      window.outboxPort, partitionValue, [operationId]);
  }, { partitionValue: receiptPartition, operationId: operationIds.durableReceipt });
  equal(await applyReceiptsFromPage(pageA, receiptPartition, [repairReceipt]), [],
    "已剪枝条目不能因迟到回执重新插入");
  equal(await readPartition(pageB, receiptPartition), [], "迟到回执不复活已清理磁盘行");
  const receiptSuccessor = await insertFromPage(pageB, receiptPartition,
    `${operationIds.durableReceipt}-successor`, "回执清理后的后继");
  assert(receiptSuccessor.client_sequence === durableSeed.client_sequence + 1,
    "回执更新与删除不得改变分区高水位");

  assert(browserErrors.length === 0, `浏览器 harness 出错: ${browserErrors.join("; ")}`);
  outcome = {
    status: "passed",
    browser: await browser.version(),
    concurrent_sequences: concurrentSequences,
    deleted_v1_next_sequence: upgrade.next_sequence,
    sequence_after_prune: afterPrune.client_sequence,
    restored_operation_ids: restored.operations.map((operation) => operation.client_operation_id),
    duplicate_operation_id_rejected: duplicateIdRejected,
    conflicting_primary_key_rejected: sequenceConflictRejected,
    durable_receipts: { aborted: abortedReceipt, restored: coldRepair.restored,
      successor_sequence: receiptSuccessor.client_sequence, late_receipt_reinserted: false },
    pending_parent_chain: { ids: chainIds, ...chain, ...recoveredChain,
      retained_after_failure_reopen: chainAfterFailure.map((record) => record.operation.client_operation_id) },
    artifacts: [
      path.relative(projectRoot, path.join(bundleDirectory, "sessionCatalogOutbox.js")),
      path.relative(projectRoot, path.join(bundleDirectory, "sessionCatalogOutboxStore.js")),
      path.relative(projectRoot, path.join(bundleDirectory, "sessionCatalogProjection.js")),
    ],
  };
  await writeFile(
    path.join(artifactDirectory, "result.json"),
    `${JSON.stringify(outcome, null, 2)}\n`,
    "utf8",
  );
  process.stdout.write(`${JSON.stringify(outcome)}\n`);
} catch (error) {
  executionError = error;
  throw error;
} finally {
  try {
    await runContext.close();
  } catch (cleanupError) {
    if (executionError) {
      throw new AggregateError([executionError, cleanupError], "IndexedDB浏览器装具执行与清理均失败");
    }
    throw cleanupError;
  }
}

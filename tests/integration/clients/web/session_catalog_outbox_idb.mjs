import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { chromium } from "playwright";

const projectRoot = process.cwd();
const artifactDirectory = path.join(
  projectRoot,
  "out/tests/integration/clients/web/session_catalog_outbox_idb/artifacts",
);
const bundleDirectory = path.join(artifactDirectory, "bundle");
await mkdir(bundleDirectory, { recursive: true });

const build = await Bun.build({
  entrypoints: [
    path.join(projectRoot, "src/clients/web/src/state/session/sessionCatalogOutbox.ts"),
    path.join(projectRoot, "src/clients/web/src/state/session/sessionCatalogOutboxStore.ts"),
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
for (const moduleName of ["sessionCatalogOutbox.js", "sessionCatalogOutboxStore.js"]) {
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
      window.outboxModules = { state, storage };
    </script>
  </body>
</html>`;

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
const operationIds = {
  upgrade: randomUUID().replaceAll("-", ""),
  first: randomUUID().replaceAll("-", ""),
  second: randomUUID().replaceAll("-", ""),
  afterPrune: randomUUID().replaceAll("-", ""),
  sequenceOriginal: randomUUID().replaceAll("-", ""),
  sequenceCollision: randomUUID().replaceAll("-", ""),
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

let outcome;
let browser;
let context;
let pageA;
let pageB;
const browserErrors = [];
try {
  browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined,
    headless: true,
  });
  context = await browser.newContext();
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
    artifacts: [
      path.relative(projectRoot, path.join(bundleDirectory, "sessionCatalogOutbox.js")),
      path.relative(projectRoot, path.join(bundleDirectory, "sessionCatalogOutboxStore.js")),
    ],
  };
  await writeFile(
    path.join(artifactDirectory, "result.json"),
    `${JSON.stringify(outcome, null, 2)}\n`,
    "utf8",
  );
  process.stdout.write(`${JSON.stringify(outcome)}\n`);
} finally {
  if (context) await context.close();
  if (browser) await browser.close();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}

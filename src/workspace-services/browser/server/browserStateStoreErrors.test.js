import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import fs from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
import path from "node:path";
import { after, test } from "node:test";

import { BrowserStateStore } from "./browserStateStore.js";

const { cp, mkdir, readFile, readdir, rm, writeFile } = fs;
const workspaceRunRoot = path.join(
  process.cwd(), "out/tests/src/workspace-services/browser/server/browserStateStoreErrors/workspace", randomUUID(),
);
after(() => rm(workspaceRunRoot, { recursive: true, force: true }));

async function createStore(t) {
  t.after(() => {
    t.mock.restoreAll();
    syncBuiltinESMExports();
  });
  const workspaceRoot = path.join(workspaceRunRoot, randomUUID());
  const workspaceId = "00000000-0000-4000-8000-000000000001";
  await cp(path.join(process.cwd(), "tests/fixtures/workspaces/default_test_workspace"), workspaceRoot, { recursive: true });
  await writeFile(path.join(workspaceRoot, ".boxteam/workspace-identity.json"), JSON.stringify({ workspace_id: workspaceId }));
  return new BrowserStateStore({ workspaceRoot, workspaceId });
}

function stateSnapshot(store, title) {
  return {
    updated_at: "2026-10-20T00:00:00.000Z",
    browsers: [{
      browser_id: `browser_${"a".repeat(32)}`,
      workspace_id: store.workspaceId,
      transport_owner_revision: 1,
      reconciliation_status: "verified",
      title,
    }],
  };
}

test("状态保存完整替换记录且不保留临时文件", async (t) => {
  const store = await createStore(t);
  await store.write(stateSnapshot(store, "第一版"));
  await store.write(stateSnapshot(store, "第二版"));
  assert.deepEqual(await store.readRecords(), stateSnapshot(store, "第二版").browsers);
  assert.deepEqual(await readdir(store.stateDir), ["browsers.json"]);
});

for (const phase of ["partial-write", "publish"]) {
  test(`状态${phase}失败保留旧记录，只清理本次文件并允许显式重试`, async (t) => {
    const store = await createStore(t);
    await store.write(stateSnapshot(store, "仍有效的旧记录"));
    const originalBytes = await readFile(store.stateFile);
    const otherTemporaryFile = path.join(store.stateDir, "another-writer.tmp");
    await writeFile(otherTemporaryFile, "其他写入者的文件", "utf8");
    const writeError = Object.assign(new Error(`模拟状态${phase}失败`), { code: "EIO" });
    if (phase === "partial-write") {
      t.mock.method(fs, "writeFile", async (file) => {
        await writeFile(file, "部分正文", "utf8");
        throw writeError;
      });
    } else {
      t.mock.method(fs, "rename", async () => { throw writeError; });
    }
    syncBuiltinESMExports();
    await assert.rejects(store.write(stateSnapshot(store, "不能发布的新记录")), (error) => {
      assert.equal(error.code, writeError.code);
      assert.equal(error.cause, writeError);
      assert(!error.message.includes(store.workspaceRoot));
      return true;
    });
    assert.deepEqual(await readFile(store.stateFile), originalBytes);
    assert.deepEqual(await store.readRecords(), stateSnapshot(store, "仍有效的旧记录").browsers);
    assert.deepEqual((await readdir(store.stateDir)).sort(), ["another-writer.tmp", "browsers.json"]);
    assert.equal(await readFile(otherTemporaryFile, "utf8"), "其他写入者的文件");
    t.mock.restoreAll();
    syncBuiltinESMExports();
    await store.write(stateSnapshot(store, "显式重试"));
    assert.deepEqual(await store.readRecords(), stateSnapshot(store, "显式重试").browsers);
    assert.deepEqual((await readdir(store.stateDir)).sort(), ["another-writer.tmp", "browsers.json"]);
  });
}

test("首次状态发布失败不生成正式文件或临时残留", async (t) => {
  const store = await createStore(t);
  const writeError = Object.assign(new Error("首次状态发布失败"), { code: "EACCES" });
  t.mock.method(fs, "rename", async () => { throw writeError; });
  syncBuiltinESMExports();
  await assert.rejects(store.write(stateSnapshot(store, "未发布")), (error) => {
      assert.equal(error.code, writeError.code);
      assert.equal(error.cause, writeError);
      assert(!error.message.includes(store.workspaceRoot));
      return true;
    });
  assert.equal(await store.readRecords(), null);
  assert.deepEqual(await readdir(store.stateDir), []);
});

test("状态发布和清理都失败时保留两份原异常，后续显式保存仍可成功", async (t) => {
  const store = await createStore(t);
  await store.write(stateSnapshot(store, "原始记录"));
  const originalBytes = await readFile(store.stateFile);
  const writeError = Object.assign(new Error("状态发布失败"), { code: "EPERM" });
  const cleanupError = Object.assign(new Error("状态临时文件清理失败"), { code: "EACCES" });
  t.mock.method(fs, "rename", async () => { throw writeError; });
  t.mock.method(fs, "rm", async () => { throw cleanupError; });
  syncBuiltinESMExports();
  await assert.rejects(store.write(stateSnapshot(store, "未发布记录")), (error) => {
    assert.equal(error.code, writeError.code);
    assert(error.cause instanceof AggregateError);
    assert.deepEqual(error.cause.errors, [writeError, cleanupError]);
    assert.equal(error.cause.cause, writeError);
    assert(!error.message.includes(store.workspaceRoot));
    return true;
  });
  assert.deepEqual(await readFile(store.stateFile), originalBytes);
  const failedFiles = (await readdir(store.stateDir)).sort();
  assert.equal(failedFiles.filter((name) => name.endsWith(".tmp")).length, 1);
  t.mock.restoreAll();
  syncBuiltinESMExports();
  await store.write(stateSnapshot(store, "下次显式保存"));
  assert.deepEqual(await store.readRecords(), stateSnapshot(store, "下次显式保存").browsers);
  // 清理失败的文件保留给诊断；后续保存不扫描或代删其他尝试的文件。
  assert.deepEqual((await readdir(store.stateDir)).sort(), failedFiles);
});

test("损坏检查点保留JSON解析异常，显式删除后读取可继续", async (t) => {
  const store = await createStore(t);
  await mkdir(store.checkpointDir, { recursive: true });
  await writeFile(store.checkpointPath("browser_broken"), "{invalid JSON");
  await assert.rejects(store.readCheckpoint("browser_broken"), (error) => {
    assert.equal(error.code, "browser_checkpoint_read_failed");
    assert(error.cause instanceof SyntaxError);
    assert.equal(error.message, "读取浏览器检查点失败: browser_id=browser_broken, stage=正文解析, code=browser_checkpoint_read_failed");
    return true;
  });
  await store.deleteCheckpoint("browser_broken");
  assert.equal(await store.readCheckpoint("browser_broken"), null);
});

test("真实目录读取失败保留文件系统错误码和原始cause", async (t) => {
  const store = await createStore(t);
  await mkdir(store.checkpointPath("browser_directory"), { recursive: true });
  await assert.rejects(store.readCheckpoint("browser_directory"), (error) => {
    assert.equal(error.code, "EISDIR");
    assert.equal(error.cause.code, "EISDIR");
    assert(error.cause instanceof Error);
    return true;
  });
});

test("发布失败保留原始异常、旧正文和配额，清理临时文件后可显式重试", async (t) => {
  const store = await createStore(t);
  const browserId = "browser_publish";
  const original = { data: "旧正文" };
  await store.writeCheckpoint(browserId, original);
  const originalBytes = await readFile(store.checkpointPath(browserId));
  const writeError = Object.assign(new Error("模拟rename失败"), { code: "EXDEV" });
  t.mock.method(fs, "rename", async () => { throw writeError; });
  syncBuiltinESMExports();
  await assert.rejects(store.writeCheckpoint(browserId, { data: "待发布正文" }), (error) => {
    assert.equal(error.code, writeError.code);
    assert.equal(error.cause, writeError);
    return true;
  });
  assert.deepEqual(await readFile(store.checkpointPath(browserId)), originalBytes);
  assert.equal((await store.checkpointBudgetSnapshot()).used_bytes, originalBytes.length);
  assert.equal((await readdir(store.checkpointDir)).filter((name) => name.endsWith(".tmp")).length, 0);
  t.mock.restoreAll();
  syncBuiltinESMExports();
  await store.writeCheckpoint(browserId, { data: "重试正文" });
  assert.deepEqual(await store.readCheckpoint(browserId), { data: "重试正文" });
});

test("发布与临时文件清理均失败时保留两份原异常和写入错误归属", async (t) => {
  const store = await createStore(t);
  const browserId = "browser_cleanup";
  const original = { data: "仍有效的旧正文" };
  await store.writeCheckpoint(browserId, original);
  const originalBytes = await readFile(store.checkpointPath(browserId));
  const writeError = Object.assign(new Error("模拟rename失败"), { code: "EXDEV" });
  const cleanupError = Object.assign(new Error("模拟临时文件删除失败"), { code: "EACCES" });
  t.mock.method(fs, "rename", async () => { throw writeError; });
  t.mock.method(fs, "rm", async () => { throw cleanupError; });
  syncBuiltinESMExports();
  await assert.rejects(store.writeCheckpoint(browserId, { data: "未发布正文" }), (error) => {
    assert.equal(error.code, writeError.code);
    assert(error.cause instanceof AggregateError);
    assert.deepEqual(error.cause.errors, [writeError, cleanupError]);
    return true;
  });
  assert.deepEqual(await readFile(store.checkpointPath(browserId)), originalBytes);
  assert.equal((await store.checkpointBudgetSnapshot()).used_bytes, originalBytes.length);
  assert.equal((await readdir(store.checkpointDir)).filter((name) => name.endsWith(".tmp")).length, 1);
  t.mock.restoreAll();
  syncBuiltinESMExports();
  await store.writeCheckpoint(browserId, { data: "显式重试成功" });
  assert.deepEqual(await store.readCheckpoint(browserId), { data: "显式重试成功" });
});

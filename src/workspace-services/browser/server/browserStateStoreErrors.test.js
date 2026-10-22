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

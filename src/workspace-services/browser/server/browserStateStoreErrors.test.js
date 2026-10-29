import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import fs from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
import path from "node:path";
import { after, test } from "node:test";
import { inspect } from "node:util";

import { BrowserStateStore } from "./browserStateStore.js";

const { mkdir, readFile, readdir, rm, writeFile } = fs;
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
  await mkdir(workspaceRoot, { recursive: true });
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

// 用真实目录/文件冲突覆盖全部存储出口；仅日志writer显式替换。
for (const operation of ["readRecords", "write", "readCheckpoint", "writeCheckpoint", "deleteCheckpoint", "checkpointBudgetSnapshot", "writeScreenshot", "writeDownload"]) {
  for (const failLog of [false, true]) {
    test(`${operation}原生失败与日志${failLog ? "次错" : "正常"}保持原归属并可重试`, async (t) => {
      const store = await createStore(t);
      const browserId = "browser_storage";
      const checkpointFile = store.checkpointPath(browserId);
      const download = {
        suggestedFilename: () => "source.txt", url: () => "https://example.test/source.txt",
        saveAs: file => writeFile(file, "原始下载字节"),
      };
      const targets = {
        readRecords: [store.stateFile, true, "读取浏览器状态失败", "正文读取"],
        write: [store.stateFile, true, "写入浏览器状态失败", "文件发布"],
        readCheckpoint: [checkpointFile, true, "读取浏览器检查点失败", "正文读取"],
        writeCheckpoint: [checkpointFile, true, "写入浏览器检查点失败", "文件发布"],
        deleteCheckpoint: [checkpointFile, true, "删除浏览器检查点失败", null],
        checkpointBudgetSnapshot: [store.checkpointDir, false, "核验浏览器检查点目录失败", "目录读取"],
        writeScreenshot: [store.screenshotDir, false, "保存浏览器截图失败", "目录创建"],
        writeDownload: [path.join(store.downloadDir, browserId), false, "保存浏览器下载失败", "目录创建"],
      };
      const [target, directory, summary, stage] = targets[operation];
      await mkdir(path.dirname(target), { recursive: true });
      if (directory) await mkdir(target);
      else await writeFile(target, "保留原冲突字节");
      const sinkError = Object.assign(new Error(`ENOTDIR: ${store.workspaceRoot}/diagnostic.log`), { code: "ENOTDIR" });
      const attempts = [];
      t.mock.method(process.stderr, "write", text => {
        attempts.push(text);
        if (failLog) throw sinkError;
      });
      const invoke = () => {
        if (operation === "write") return store.write(stateSnapshot(store, "新记录"));
        if (operation === "writeCheckpoint") return store.writeCheckpoint(browserId, { data: "新检查点" });
        if (operation === "writeScreenshot") return store.writeScreenshot(browserId, Buffer.from("截图字节"));
        if (operation === "writeDownload") return store.writeDownload(browserId, download);
        return store[operation](browserId);
      };
      await assert.rejects(invoke(), error => {
        assert(error.message.startsWith(`${summary}: `));
        assert(!error.message.includes(store.workspaceRoot));
        if (stage) assert(error.message.includes(`stage=${stage}`));
        const primary = failLog ? error.cause.errors[0] : error.cause;
        assert(primary instanceof Error);
        assert.equal(error.code, primary.code);
        assert.equal(attempts.length, 1);
        assert(attempts[0].includes(inspect(primary)));
        assert(attempts[0].includes(target));
        if (failLog) {
          assert(error.cause instanceof AggregateError);
          assert.deepEqual(error.cause.errors, [primary, sinkError]);
          assert.equal(error.cause.cause, primary);
        }
        return true;
      });
      assert.deepEqual(directory ? await readdir(target) : await readFile(target, "utf8"), directory ? [] : "保留原冲突字节");
      t.mock.restoreAll();
      await rm(target, { recursive: true });
      const retried = await invoke();
      if (operation === "readRecords" || operation === "readCheckpoint") assert.equal(retried, null);
      if (operation === "write") assert.deepEqual(await store.readRecords(), stateSnapshot(store, "新记录").browsers);
      if (operation === "writeCheckpoint") assert.deepEqual(await store.readCheckpoint(browserId), { data: "新检查点" });
      if (operation === "checkpointBudgetSnapshot") assert.equal(retried.used_bytes, 0);
      if (operation === "writeScreenshot") assert.equal(retried.byte_length, Buffer.byteLength("截图字节"));
      if (operation === "writeDownload") {
        assert.equal(retried.status, "completed");
        assert.equal(await readFile(path.join(store.downloadDir, browserId, `${retried.download_id}-source.txt`), "utf8"), "原始下载字节");
      }
    });
  }
}

for (const checkpoint of [false, true]) {
  test(`${checkpoint ? "检查点" : "状态"}发布、清理与诊断三错保留顺序、旧正文和下一次保存`, async (t) => {
    const store = await createStore(t);
    const browserId = "browser_triple";
    const save = title => checkpoint ? store.writeCheckpoint(browserId, { title }) : store.write(stateSnapshot(store, title));
    const file = checkpoint ? store.checkpointPath(browserId) : store.stateFile;
    await save("已发布版本");
    const oldBytes = await readFile(file);
    const writeError = Object.assign(new Error("/private/publish-failure"), { code: "EXDEV" });
    const cleanupError = Object.assign(new Error(String.raw`C:\private\cleanup-failure`), { code: "EACCES" });
    const sinkError = new Error(String.raw`\\host\share\diagnostic-failure`);
    t.mock.method(fs, "rename", async () => { throw writeError; });
    t.mock.method(fs, "rm", async () => { throw cleanupError; });
    t.mock.method(process.stderr, "write", () => { throw sinkError; });
    syncBuiltinESMExports();
    await assert.rejects(save("不能发布"), error => {
      assert.equal(error.code, writeError.code);
      assert(error.cause instanceof AggregateError);
      const [publicationFailure, diagnosticFailure] = error.cause.errors;
      assert(publicationFailure instanceof AggregateError);
      assert.deepEqual(publicationFailure.errors, [writeError, cleanupError]);
      assert.equal(error.cause.cause, publicationFailure);
      assert.equal(diagnosticFailure, sinkError);
      for (const original of [writeError, cleanupError, sinkError]) assert(!error.message.includes(original.message));
      return true;
    });
    assert.deepEqual(await readFile(file), oldBytes);
    t.mock.restoreAll();
    syncBuiltinESMExports();
    if (checkpoint) assert.equal((await store.checkpointBudgetSnapshot()).used_bytes, oldBytes.length);
    const remnants = (await readdir(path.dirname(file))).filter(name => name.endsWith(".tmp"));
    assert.equal(remnants.length, 1);
    await save("显式重试");
    assert.deepEqual(checkpoint ? await store.readCheckpoint(browserId) : await store.readRecords(), checkpoint ? { title: "显式重试" } : stateSnapshot(store, "显式重试").browsers);
    assert.deepEqual((await readdir(path.dirname(file))).filter(name => name.endsWith(".tmp")), remnants);
  });
}

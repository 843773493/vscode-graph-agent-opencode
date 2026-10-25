import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import fs from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
import path from "node:path";
import { after, test } from "node:test";

import { TerminalStateStore } from "./terminalStateStore.js";

const { mkdir, readFile, readdir, rm, writeFile } = fs;
const workspaceRunRoot = path.join(
  process.cwd(), "out/tests/src/workspace-services/terminal/server/terminalStateStore/workspace", randomUUID(),
);
after(() => rm(workspaceRunRoot, { recursive: true, force: true }));

async function createStore(t) {
  t.after(() => {
    t.mock.restoreAll();
    syncBuiltinESMExports();
  });
  const workspaceRoot = path.join(workspaceRunRoot, randomUUID());
  await mkdir(workspaceRoot, { recursive: true });
  return new TerminalStateStore({ workspaceRoot, workspaceId: "00000000-0000-4000-8000-000000000001" });
}

function snapshot(label) {
  return { updated_at: "2026-10-20T00:00:00.000Z", terminals: [{ terminal_id: "term_state", title: label }] };
}

test("正常发布替换完整记录且不保留临时文件", async (t) => {
  const store = await createStore(t);
  await store.write(snapshot("第一版"));
  await store.write(snapshot("第二版"));
  assert.deepEqual(await store.readRecords(), snapshot("第二版").terminals);
  assert.deepEqual(await readdir(store.stateDir), ["terminals.json"]);
});

for (const phase of ["partial-write", "publish"]) {
  test(`${phase}失败保留旧字节与原异常，删除临时文件后可显式重试`, async (t) => {
    const store = await createStore(t);
    await store.write(snapshot("仍有效的原始记录"));
    const originalBytes = await readFile(store.stateFile);
    const writeError = Object.assign(new Error(`模拟${phase}失败`), { code: "EIO" });
    if (phase === "partial-write") {
      t.mock.method(fs, "writeFile", async (file) => {
        await writeFile(file, "部分正文", "utf8");
        throw writeError;
      });
    } else {
      t.mock.method(fs, "rename", async () => { throw writeError; });
    }
    syncBuiltinESMExports();
    await assert.rejects(store.write(snapshot("不能发布的新记录")), (error) => {
      assert.equal(error.cause, writeError);
      assert.equal(error.code, "EIO");
      assert.equal(error.message, `写入终端状态失败: workspace_id=${store.workspaceId}, artifact=terminal_records, stage=${phase === "partial-write" ? "正文写入" : "文件发布"}, code=EIO`);
      return true;
    });
    assert.deepEqual(await readFile(store.stateFile), originalBytes);
    assert.deepEqual(await store.readRecords(), snapshot("仍有效的原始记录").terminals);
    assert.deepEqual(await readdir(store.stateDir), ["terminals.json"]);
    t.mock.restoreAll();
    syncBuiltinESMExports();
    await store.write(snapshot("显式重试"));
    assert.deepEqual(await store.readRecords(), snapshot("显式重试").terminals);
  });
}

test("首次发布失败不生成正式状态或临时残留", async (t) => {
  const store = await createStore(t);
  const writeError = Object.assign(new Error("首次发布失败"), { code: "EACCES" });
  t.mock.method(fs, "rename", async () => { throw writeError; });
  syncBuiltinESMExports();
  await assert.rejects(store.write(snapshot("未发布")), (error) => {
    assert.equal(error.cause, writeError);
    assert.equal(error.code, "EACCES");
    assert(error.message.includes("stage=文件发布"));
    return true;
  });
  assert.equal(await store.readRecords(), null);
  assert.deepEqual(await readdir(store.stateDir), []);
});

test("发布和清理都失败时保留两个原异常，后续保存仍可成功", async (t) => {
  const store = await createStore(t);
  await store.write(snapshot("原始记录"));
  const originalBytes = await readFile(store.stateFile);
  const writeError = Object.assign(new Error("发布失败"), { code: "EPERM" });
  const cleanupError = Object.assign(new Error("临时文件清理失败"), { code: "EACCES" });
  t.mock.method(fs, "rename", async () => { throw writeError; });
  t.mock.method(fs, "rm", async () => { throw cleanupError; });
  syncBuiltinESMExports();
  await assert.rejects(store.write(snapshot("未发布记录")), (error) => {
    assert.equal(error.code, "EPERM");
    assert(error.message.includes("stage=文件发布"));
    assert(error.cause instanceof AggregateError);
    assert.deepEqual(error.cause.errors, [writeError, cleanupError]);
    assert.equal(error.cause.cause, writeError);
    return true;
  });
  assert.deepEqual(await readFile(store.stateFile), originalBytes);
  assert.equal((await readdir(store.stateDir)).filter((name) => name.endsWith(".tmp")).length, 1);
  t.mock.restoreAll();
  syncBuiltinESMExports();
  await store.write(snapshot("下次显式保存"));
  assert.deepEqual(await store.readRecords(), snapshot("下次显式保存").terminals);
  assert.deepEqual(await readdir(store.stateDir), ["terminals.json"]);
});

for (const mode of ["directory", "parse", "schema", "workspace", "records"]) {
  test(`读取${mode}失败只发布身份/阶段，修复原文件后可重试`, async (t) => {
    const store = await createStore(t);
    const value = snapshot("原始状态");
    await store.write(value);
    const originalBytes = await readFile(store.stateFile);
    if (mode === "directory") {
      await rm(store.stateFile);
      await mkdir(store.stateFile);
    } else if (mode === "parse") {
      await writeFile(store.stateFile, `{"invalid": ${store.stateFile}`);
    } else {
      const payload = JSON.parse(originalBytes);
      payload[mode === "schema" ? "schema_version" : mode === "workspace" ? "workspace_id" : "terminals"] = store.stateFile;
      await writeFile(store.stateFile, JSON.stringify(payload));
    }
    const logged = t.mock.method(console, "error", () => undefined);
    await assert.rejects(store.readRecords(), (error) => {
      const stage = mode === "directory" ? "正文读取" : mode === "parse" ? "正文解析" : "记录核验";
      assert.equal(error.message, `读取终端状态失败: workspace_id=${store.workspaceId}, artifact=terminal_records, stage=${stage}, code=${error.code}`);
      assert(!error.message.includes(store.stateFile));
      assert.equal(error.code, mode === "directory" ? "EISDIR" : "terminal_records_read_failed");
      assert(logged.mock.calls.some(({ arguments: args }) => args.at(-1) === error.cause && args[1].filePath === store.stateFile));
      return true;
    });
    if (mode === "directory") await rm(store.stateFile, { recursive: true });
    await writeFile(store.stateFile, originalBytes);
    assert.deepEqual(await store.readRecords(), value.terminals);
  });
}

test("目录创建失败不能当作状态缺失，读写只发布各自阶段且不清理别的文件", async (t) => {
  const store = await createStore(t);
  await mkdir(path.dirname(store.stateDir), { recursive: true });
  await writeFile(store.stateDir, "目录占位正文");
  for (const operation of [() => store.readRecords(), () => store.write(snapshot("未发布"))]) {
    await assert.rejects(operation(), (error) => {
      assert.equal(error.code, "EEXIST");
      assert(error.message.includes("stage=目录创建"));
      assert(!error.message.includes(store.stateDir));
      assert.equal(error.cause.path, store.stateDir);
      return true;
    });
    assert.equal(await readFile(store.stateDir, "utf8"), "目录占位正文");
  }
});

test("编码失败保留已发布状态且不把序列化异常正文公开", async (t) => {
  const store = await createStore(t);
  await store.write(snapshot("已发布"));
  const originalBytes = await readFile(store.stateFile);
  const circular = {};
  circular[store.stateFile] = circular;
  await assert.rejects(store.write({ ...snapshot("未发布"), terminals: [circular] }), (error) => {
    assert.equal(error.code, "terminal_records_write_failed");
    assert(error.message.includes("stage=正文编码"));
    assert(!error.message.includes(store.stateFile));
    assert(error.cause.message.includes(store.stateFile));
    return true;
  });
  assert.deepEqual(await readFile(store.stateFile), originalBytes);
  assert.deepEqual(await readdir(store.stateDir), ["terminals.json"]);
});

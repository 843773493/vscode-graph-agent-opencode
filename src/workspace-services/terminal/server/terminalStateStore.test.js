import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import fs from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
import path from "node:path";
import { after, test } from "node:test";

import { TerminalStateStore } from "./terminalStateStore.js";

const { cp, readFile, readdir, rm, writeFile } = fs;
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
  await cp(path.join(process.cwd(), "tests/fixtures/workspaces/default_test_workspace"), workspaceRoot, { recursive: true });
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
    await assert.rejects(store.write(snapshot("不能发布的新记录")), (error) => error === writeError);
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
  await assert.rejects(store.write(snapshot("未发布")), (error) => error === writeError);
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
    assert(error instanceof AggregateError);
    assert.deepEqual(error.errors, [writeError, cleanupError]);
    assert.equal(error.cause, writeError);
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

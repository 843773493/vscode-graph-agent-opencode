import { inspect } from "node:util";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import { mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { after, test } from "node:test";
import { TerminalStateStore } from "./terminalStateStore.js";

const workspaceRunRoot = path.join(process.cwd(), "out/tests/src/workspace-services/terminal/server/terminalStateStore/workspace", randomUUID());
after(() => rm(workspaceRunRoot, { recursive: true, force: true }));
async function createStore(t) {
  const workspaceRoot = path.join(workspaceRunRoot, randomUUID());
  await mkdir(workspaceRoot, { recursive: true });
  const store = new TerminalStateStore({ workspaceRoot, workspaceId: "00000000-0000-4000-8000-000000000001" });
  t.after(() => store.close());
  return store;
}
const snapshot = (...terminals) => ({ updated_at: "2026-10-20T00:00:00.000Z", terminals });
const record = (id, title) => ({ terminal_id: id, title });

test("批次原子提交更新、删除和顺序，重开保留完整正文", async t => {
  const store = await createStore(t);
  const first = record("term_first", "正文一"), second = record("term_second", "正文二");
  await store.write(snapshot(first, second, record("term_removed", "待删除")));
  await store.write(snapshot({ ...second, title: "正文二已更新" }, { ...first }));
  await store.close();
  assert.deepEqual(await store.readRecords(), [{ ...second, title: "正文二已更新" }, first]);
});

test("后项写入失败回滚整批，日志次错保留原失败，原调用可以显式重试", async t => {
  const store = await createStore(t);
  const first = record("first", "已确认"), second = record("second", "保持原值");
  await store.write(snapshot(first, second));
  const db = new DatabaseSync(store.stateFile);
  try {
    db.exec("CREATE TRIGGER fail_second BEFORE UPDATE ON terminal_records WHEN old.terminal_id='second' BEGIN SELECT RAISE(ABORT,'/private/storage-failure'); END");
    const diagnostic = new Error("/private/log-failure");
    const logged = t.mock.method(process.stderr, "write", () => { throw diagnostic; });
    await assert.rejects(store.write(snapshot(record("first", "未确认"), record("second", "失败"))), error => {
      assert.equal(error.code, "ERR_SQLITE_ERROR");
      assert(error.message.includes("stage=事务写入"));
      assert(!error.message.includes("/private/"));
      assert(error.cause instanceof AggregateError);
      assert.equal(error.cause.errors[1], diagnostic);
      assert(error.cause.errors[0].message.includes("/private/storage-failure"));
      assert.equal(logged.mock.callCount(), 1);
      return true;
    });
    logged.mock.restore();
    assert.deepEqual(await store.readRecords(), [first, second]);
    db.exec("DROP TRIGGER fail_second");
    await store.write(snapshot(record("first", "重试成功")));
    assert.deepEqual(await store.readRecords(), [record("first", "重试成功")]);
  } finally { db.close(); }
});

test("数据库忙时主线程仍可推进并释放原锁", async t => {
  const store = await createStore(t);
  await store.write(snapshot(record("term_busy", "原状态")));
  const db = new DatabaseSync(store.stateFile);
  try {
    db.exec("BEGIN IMMEDIATE");
    const saving = store.write(snapshot(record("term_busy", "待确认")));
    await new Promise(resolve => setTimeout(resolve, 30));
    db.exec("ROLLBACK");
    await saving;
    assert.deepEqual(await store.readRecords(), [record("term_busy", "待确认")]);
  } finally { db.close(); }
});

for (const mode of ["directory", "bytes", "schema", "workspace"]) {
  test(`冷读取${mode}损坏明确报错，原始位置只留诊断`, async t => {
    const store = await createStore(t);
    await store.write(snapshot(record("term_state", "原状态")));
    await store.close();
    if (mode === "directory") { await rm(store.stateFile); await mkdir(store.stateFile); }
    else if (mode === "bytes") await writeFile(store.stateFile, "损坏数据库");
    else {
      const db = new DatabaseSync(store.stateFile);
      if (mode === "schema") db.exec("PRAGMA user_version=99");
      else db.prepare("UPDATE terminal_state SET workspace_id=?").run(store.stateFile);
      db.close();
    }
    const logged = t.mock.method(process.stderr, "write", () => true);
    await assert.rejects(store.readRecords(), error => {
      assert(error.message.startsWith(`读取终端状态失败: workspace_id=${store.workspaceId}, artifact=terminal_records, stage=`));
      assert(!error.message.includes(store.stateFile));
      assert(logged.mock.calls.some(({ arguments: args }) => args[0].includes(inspect(store.stateFile)) && args[0].includes(error.cause.message)));
      return true;
    });
  });
}

test("旧JSON不会自动读写；显式导入后只读SQLite，重复导入不覆盖", async t => {
  const store = await createStore(t);
  await mkdir(store.stateDir, { recursive: true });
  const legacyFile = path.join(store.stateDir, "terminals.json");
  const original = JSON.stringify({ schema_version: 3, workspace_id: store.workspaceId, ...snapshot({
    ...record("term_imported", "保留原身份与正文"), workspace_id: store.workspaceId,
    cwd_relative: ".", transport_owner_revision: 2, reconciliation_status: "unknown", buffer: "旧输出\r\n", os_pid: 123,
  }) });
  await writeFile(legacyFile, original);
  await assert.rejects(store.readRecords(), error => error.cause.message.includes("显式导入"));
  assert.deepEqual(await readdir(store.stateDir), ["terminals.json"]);
  assert.deepEqual(await store.request("import"), { terminals: 1 });
  const imported = await store.readRecords();
  assert.deepEqual(imported, JSON.parse(original).terminals);
  await assert.rejects(store.request("import"), error => error.cause.message.includes("已存在"));
  assert.equal(await readFile(legacyFile, "utf8"), original);
  await writeFile(legacyFile, "不会再读取的离线原件");
  await store.close();
  assert.deepEqual(await store.readRecords(), imported);
});

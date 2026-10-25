import assert from "node:assert/strict";
import path from "node:path";
import { cpSync, mkdirSync, rmSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import test from "node:test";

import { TerminalManager } from "./terminalManager.js";
import { IsolatedPtyProcess } from "./processes/isolatedPtyProcess.js";

test("外部绝对 cwd 经真实创建、落盘和恢复后仍传给同一 PTY 目录", async (t) => {
  const testRunRoot = path.join(process.cwd(), "out/tests/src/workspace-services/terminal/server/terminalCwdPersistence/workspace", randomUUID());
  t.after(() => rmSync(testRunRoot, { recursive: true, force: true }));
  const workspaceRoot = path.join(testRunRoot, "owner");
  const externalCwd = path.join(testRunRoot, "outside");
  cpSync(path.join(process.cwd(), "tests/fixtures/workspaces/terminal_cwd_workspace"), workspaceRoot, { recursive: true });
  mkdirSync(externalCwd, { recursive: true });
  const { workspace_id: workspaceId } = JSON.parse(await readFile(path.join(workspaceRoot, ".boxteam", "workspace-identity.json"), "utf8"));
  const terminalId = "term_0123456789abcdef0123456789abcdef";
  const workerDirectories = [];
  t.mock.method(IsolatedPtyProcess.prototype, "start", async ({ options }) => {
    workerDirectories.push(options.cwd);
    return { pid: null };
  });
  t.mock.method(IsolatedPtyProcess.prototype, "shutdown", async () => {});
  const manager = new TerminalManager({ workspaceRoot, workspaceId });
  t.after(() => manager.shutdown());
  await manager.init();
  await manager.create({ workspaceId, terminalId, ownerRevision: 1, cwd: externalCwd });
  assert.deepEqual(workerDirectories, [externalCwd]);
  await manager.shutdown();

  const raw = await readFile(path.join(workspaceRoot, ".boxteam", "terminal-manager", "terminals.json"), "utf8");
  const [persistedRecord] = JSON.parse(raw).terminals;
  assert.equal(persistedRecord.cwd_relative, path.relative(workspaceRoot, externalCwd));
  assert.ok(persistedRecord.cwd_relative.startsWith(`..${path.sep}`));
  assert.equal("cwd" in persistedRecord, false);
  assert.equal(raw.includes(workspaceRoot), false);
  assert.equal(raw.includes(externalCwd), false);

  const restoredManager = new TerminalManager({ workspaceRoot, workspaceId });
  t.after(() => restoredManager.shutdown());
  await restoredManager.init();
  assert.equal(restoredManager.get(terminalId).cwd, externalCwd);
  await restoredManager.get(terminalId).start();
  assert.deepEqual(workerDirectories, [externalCwd, externalCwd]);
});

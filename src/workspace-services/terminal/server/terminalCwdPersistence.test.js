import assert from "node:assert/strict";
import path from "node:path";
import { cpSync, rmSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import test from "node:test";

import { TerminalManager } from "./terminalManager.js";
import { IsolatedPtyProcess } from "./processes/isolatedPtyProcess.js";

test("外部绝对 cwd 经真实创建、落盘和恢复后仍传给同一 PTY 目录", async (t) => {
  const runRoot = path.join(process.cwd(), "out/tests/src/workspace-services/terminal/server/terminalCwdPersistence/workspace", randomUUID());
  t.after(() => rmSync(runRoot, { recursive: true, force: true }));
  const root = path.join(runRoot, "owner");
  const outside = path.join(runRoot, "outside");
  for (const directory of [root, outside]) {
    cpSync(path.join(process.cwd(), "tests/fixtures/workspaces/default_test_workspace"), directory, { recursive: true });
  }
  const { workspace_id: workspaceId } = JSON.parse(await readFile(path.join(root, ".boxteam", "workspace-identity.json"), "utf8"));
  const terminalId = "term_0123456789abcdef0123456789abcdef";
  const workerDirectories = [];
  t.mock.method(IsolatedPtyProcess.prototype, "start", async ({ options }) => {
    workerDirectories.push(options.cwd);
    return { pid: null };
  });
  t.mock.method(IsolatedPtyProcess.prototype, "shutdown", async () => {});
  const manager = new TerminalManager({ workspaceRoot: root, workspaceId });
  t.after(() => manager.shutdown());
  await manager.init();
  await manager.create({ workspaceId, terminalId, ownerRevision: 1, cwd: outside });
  assert.deepEqual(workerDirectories, [outside]);
  await manager.shutdown();

  const raw = await readFile(path.join(root, ".boxteam", "terminal-manager", "terminals.json"), "utf8");
  const [record] = JSON.parse(raw).terminals;
  assert.equal(record.cwd_relative, path.relative(root, outside));
  assert.ok(record.cwd_relative.startsWith(`..${path.sep}`));
  assert.equal("cwd" in record, false);
  assert.equal(raw.includes(root), false);
  assert.equal(raw.includes(outside), false);

  const restored = new TerminalManager({ workspaceRoot: root, workspaceId });
  t.after(() => restored.shutdown());
  await restored.init();
  assert.equal(restored.get(terminalId).cwd, outside);
  await restored.get(terminalId).start();
  assert.deepEqual(workerDirectories, [outside, outside]);
});

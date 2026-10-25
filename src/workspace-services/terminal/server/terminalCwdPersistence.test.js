import assert from "node:assert/strict";
import path from "node:path";
import { cpSync, mkdirSync, rmSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { pathToFileURL } from "node:url";
import test from "node:test";

import { TerminalManager } from "./terminalManager.js";
import { IsolatedPtyProcess } from "./processes/isolatedPtyProcess.js";

test("真实线程池排队中的cwd检查不阻断同进程HTTP，原结果在排队结束后交付", async (t) => {
  const root = path.join(process.cwd(), "out/tests/src/workspace-services/terminal/server/terminalCwdPersistence/workspace", randomUUID());
  t.after(() => rmSync(root, { recursive: true, force: true }));
  cpSync(path.join(process.cwd(), "tests/fixtures/workspaces/terminal_cwd_workspace"), root, { recursive: true });
  const moduleUrl = pathToFileURL(path.join(process.cwd(), "src/workspace-services/terminal/server/terminalSession.js")).href;
  const script = `
import assert from "node:assert/strict";
import { createServer, get } from "node:http";
import { pbkdf2 } from "node:crypto";
import { promisify } from "node:util";
import { resolveTerminalCwd } from ${JSON.stringify(moduleUrl)};
const server = createServer((_, response) => response.end("peer-ready"));
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
// 单线程池内先提交真实计算，随后stat只能排队；HTTP使用数字loopback地址。
const busy = promisify(pbkdf2)("terminal-cwd", "threadpool", 2_000_000, 32, "sha256");
let cwdDelivered = false;
const cwd = Promise.resolve(resolveTerminalCwd(process.argv[1], ".")).then(value => { cwdDelivered = true; return value; });
try {
  const body = await new Promise((resolve, reject) => {
    get({ host: "127.0.0.1", port: server.address().port }, response => {
      let body = "";
      response.on("data", chunk => { body += chunk; });
      response.on("end", () => resolve(body));
      response.on("error", reject);
    }).on("error", reject);
  });
  assert.equal(body, "peer-ready");
  assert.equal(cwdDelivered, false, "排队stat尚未交付时HTTP已完成");
  await busy;
  assert.equal(await cwd, process.argv[1]);
} finally {
  await Promise.allSettled([busy, cwd]);
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
}
`;
  await promisify(execFile)(process.execPath, ["--input-type=module", "-e", script, root], {
    cwd: process.cwd(), env: { ...process.env, UV_THREADPOOL_SIZE: "1" }, timeout: 15000,
  });
});

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

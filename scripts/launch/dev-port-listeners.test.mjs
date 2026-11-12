import { describe, expect, test } from "bun:test";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import path from "node:path";
import vm from "node:vm";
import ts from "typescript";

import { remainingOccupiedPorts } from "./dev-environment.mjs";

const runId = randomUUID();
const outputRoot = path.resolve("out/tests/scripts/launch/dev-port-listeners");
const workspaceRoot = path.join(outputRoot, "workspace");
const artifactRoot = path.join(outputRoot, "artifacts", runId);
mkdirSync(workspaceRoot, { recursive: true });
mkdirSync(artifactRoot, { recursive: true });

const source = readFileSync(path.resolve("scripts/launch/dev.mjs"), "utf8");
const ast = ts.createSourceFile("dev.mjs", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const extracted = ["readUnixListenerPidsByPort", "assertDevelopmentPortsFree", "assertDevelopmentPortsAvailable"]
  .map((name) => {
    const node = ast.statements.find((entry) => ts.isFunctionDeclaration(entry) && entry.name?.text === name);
    if (!node) throw new Error(`dev.mjs 缺少 ${name}`);
    return node.getText(ast);
  }).join("\n");

function harness(spawnSync, { ports, kill = () => { throw new Error("不得终止任何监听者"); } }) {
  return vm.runInNewContext(`${extracted}\n({ readUnixListenerPidsByPort, assertDevelopmentPortsAvailable })`, {
    projectRoot: workspaceRoot,
    process: { platform: process.platform === "win32" ? "linux" : process.platform, kill },
    TextDecoder,
    selectedPorts: () => ports,
    remainingOccupiedPorts,
    Bun: { spawnSync, sleep: async () => { throw new Error("不得进入终止等待"); } },
  });
}

function save(name, record) {
  writeFileSync(path.join(artifactRoot, `${name}.json`), `${JSON.stringify(record, null, 2)}\n`, { flag: "wx" });
}

describe("Unix 开发端口保护", () => {
  test.skipIf(process.platform === "win32" || Bun.which("lsof") === null)("真实监听端口拒绝启动且不向监听进程发信号", async () => {
    const server = createServer();
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
    const port = server.address().port;
    const calls = [];
    const api = harness((command, options) => {
      calls.push(command);
      return Bun.spawnSync(command, options);
    }, { ports: [port] });
    let error;
    try { api.assertDevelopmentPortsAvailable(); } catch (cause) { error = cause; }
    await new Promise((resolve, reject) => server.close((cause) => cause ? reject(cause) : resolve()));
    save("active-listener-refused", { port, pid: process.pid, calls, error: error?.message });
    expect(error?.message).toContain("无法确认属于当前实例");
    expect(error?.message).toContain(`pid=${process.pid}`);
    expect(calls).toHaveLength(1);
  }, 5000);

  test("空闲端口可用，lsof 查询失败则响亮失败", async () => {
    const calls = [];
    const api = harness((command) => {
      calls.push(command);
      return { exitCode: 1, signalCode: null, stdout: Buffer.from(""), stderr: Buffer.from("") };
    }, { ports: [23947] });
    api.assertDevelopmentPortsAvailable();
    save("empty-listener-set", { calls });
    expect(calls).toHaveLength(1);
  });

  test("lsof 错误不被当作端口空闲", async () => {
    const api = harness(() => ({ exitCode: 2, signalCode: null, stdout: Buffer.from(""), stderr: Buffer.from("fixture failure") }), { ports: [23947] });
    expect(() => api.assertDevelopmentPortsAvailable()).toThrow("fixture failure");
  });
});

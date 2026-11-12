import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import ts from "typescript";
import { remainingOccupiedPorts } from "./dev-environment.mjs";

const outputRoot = path.resolve("out/tests/scripts/launch/dev-windows-port-listeners");
const root = path.join(outputRoot, "workspace");
const artifacts = path.join(outputRoot, "artifacts", randomUUID());
mkdirSync(root, { recursive: true });
mkdirSync(artifacts, { recursive: true });
const source = readFileSync("scripts/launch/dev.mjs", "utf8");
const ast = ts.createSourceFile("dev.mjs", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const code = ["readWindowsListenerPidsByPort", "assertDevelopmentPortsFree", "assertDevelopmentPortsAvailable"].map((name) => {
  const node = ast.statements.find((entry) => ts.isFunctionDeclaration(entry) && entry.name?.text === name);
  if (!node) throw new Error(`dev.mjs 缺少 ${name}`);
  return node.getText(ast);
}).join("\n");

function createHarness(responses) {
  const commands = [];
  const api = vm.runInNewContext(`${code}\n({ assertDevelopmentPortsAvailable })`, {
    projectRoot: root,
    process: { platform: "win32" },
    selectedPorts: () => [8010, 8014],
    remainingOccupiedPorts,
    TextDecoder,
    Bun: { spawnSync(command) {
      commands.push(command);
      const result = responses.shift();
      if (!result) throw new Error("unexpected command");
      if (command[0] === "taskkill") throw new Error("taskkill must never run");
      return result;
    } },
  });
  return { api, commands };
}

describe("Windows 开发端口保护", () => {
  test("目标端口有 listener 时拒绝启动，不调用 taskkill", async () => {
    const { api, commands } = createHarness([{ exitCode: 0, signalCode: null,
      stdout: Buffer.from("TCP 127.0.0.1:8014 0.0.0.0:0 LISTENING 4012\r\n"), stderr: Buffer.from("") }]);
    expect(() => api.assertDevelopmentPortsAvailable()).toThrow("不会终止这些进程");
    expect(commands).toEqual([["netstat", "-ano", "-p", "tcp"]]);
    writeFileSync(path.join(artifacts, "occupied.json"), `${JSON.stringify({ commands })}\n`);
  });

  test("查询失败时不继续启动", async () => {
    const { api, commands } = createHarness([{ exitCode: 2, signalCode: null,
      stdout: Buffer.from(""), stderr: Buffer.from("fixture error") }]);
    expect(() => api.assertDevelopmentPortsAvailable()).toThrow("fixture error");
    expect(commands).toHaveLength(1);
  });
});

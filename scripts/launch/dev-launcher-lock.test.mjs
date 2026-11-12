import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import ts from "typescript";

const output = path.resolve("out/tests/scripts/launch/dev-launcher-lock");
const workspace = path.join(output, "workspace", randomUUID());
const artifacts = path.join(output, "artifacts", randomUUID());
const home = path.join(workspace, "home");
mkdirSync(path.join(home, "state"), { recursive: true });
mkdirSync(artifacts, { recursive: true });
const source = readFileSync("scripts/launch/dev.mjs", "utf8");
const ast = ts.createSourceFile("dev.mjs", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const functions = ["launcherLockPid", "assertNoExistingLauncher"].map((name) => {
  const node = ast.statements.find((entry) => ts.isFunctionDeclaration(entry) && entry.name?.text === name);
  if (!node) throw new Error(`dev.mjs 缺少 ${name}`);
  return node.getText(ast);
}).join("\n");

function createAssertion(kill) {
  return vm.runInNewContext(`${functions}\nassertNoExistingLauncher()`, {
    boxteamHome: home,
    path,
    existsSync: (file) => file === path.join(home, "state", "launcher.lock"),
    readFileSync,
    process: { kill },
  });
}

describe("dev.mjs Launcher 生命周期保护", () => {
  test("活跃 Launcher 只探测存活并拒绝启动，不发送终止信号", () => {
    writeFileSync(path.join(home, "state", "launcher.lock"), JSON.stringify({ pid: 4132 }));
    const calls = [];
    expect(() => createAssertion((pid, signal) => calls.push([pid, signal]))).toThrow("请先使用对应实例");
    expect(calls).toEqual([[4132, 0]]);
    writeFileSync(path.join(artifacts, "active-lock.json"), `${JSON.stringify({ pid: 4132, calls })}\n`);
  });

  test("仅明确 ESRCH 的旧锁允许继续启动", () => {
    const gone = Object.assign(new Error("gone"), { code: "ESRCH" });
    writeFileSync(path.join(home, "state", "launcher.lock"), JSON.stringify({ pid: 4133 }));
    expect(() => createAssertion(() => { throw gone; })).not.toThrow();
  });

  test("其它进程查询错误原样显露", () => {
    writeFileSync(path.join(home, "state", "launcher.lock"), JSON.stringify({ pid: 4134 }));
    const denied = Object.assign(new Error("permission denied"), { code: "EPERM" });
    expect(() => createAssertion(() => { throw denied; })).toThrow("permission denied");
  });
});

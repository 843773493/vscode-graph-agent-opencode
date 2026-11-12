import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const source = ts.createSourceFile("verify-windows-x64.mjs",
  readFileSync(new URL("./verify-windows-x64.mjs", import.meta.url), "utf8"),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const declaration = source.statements.find(node => ts.isFunctionDeclaration(node)
  && node.name?.text === "stopWindowsProcessTree");

function cleanup(result, probeError) {
  const commands = [];
  const probes = [];
  const context = vm.createContext({
    spawnSync(command, args, options) {
      commands.push({ command, args: Array.from(args), timeout: options.timeout });
      return result;
    },
    process: { kill(pid, signal) {
      probes.push([pid, signal]);
      if (probeError) throw probeError;
    } },
  });
  vm.runInContext(declaration.getText(source), context);
  return { stop: () => context.stopWindowsProcessTree(12345), commands, probes };
}

test("taskkill 成功直接返回并保留外部命令期限", () => {
  const run = cleanup({ status: 0 });
  run.stop();
  expect(run.probes).toEqual([]);
  expect(run.commands).toEqual([{ command: "taskkill.exe", args: ["/T", "/F", "/PID", "12345"], timeout: 5000 }]);
});

for (const message of ['错误: 没有找到进程 "12345"。', 'ERROR: The process "12345" not found.']) {
  test(`进程已消失时忽略本地化 taskkill 非零结果：${message}`, () => {
    const run = cleanup({ status: 128, stderr: message }, Object.assign(new Error("gone"), { code: "ESRCH" }));
    run.stop();
    expect(run.probes).toEqual([[12345, 0]]);
  });
  test(`进程仍在时不能靠错误文本假定成功：${message}`, () => {
    const run = cleanup({ status: 128, stderr: message });
    expect(run.stop).toThrow(message);
    expect(run.probes).toEqual([[12345, 0]]);
  });
}

test("进程查询权限错误原样暴露", () => {
  const error = Object.assign(new Error("access denied"), { code: "EPERM" });
  const run = cleanup({ status: 1, stderr: "拒绝访问" }, error);
  let caught;
  try { run.stop(); } catch (failure) { caught = failure; }
  expect(caught).toBe(error);
});

for (const code of ["ENOENT", "ETIMEDOUT"]) {
  test(`taskkill 创建或超时错误 ${code} 不能被当作已退出`, () => {
    const error = Object.assign(new Error(code), { code });
    const run = cleanup({ status: null, error }, Object.assign(new Error("gone"), { code: "ESRCH" }));
    let caught;
    try { run.stop(); } catch (failure) { caught = failure; }
    expect(caught).toBe(error);
    expect(run.probes).toEqual([]);
  });
}

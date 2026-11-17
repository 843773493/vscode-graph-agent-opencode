import { expect, test } from "bun:test";
import { EventEmitter, once } from "node:events";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const source = ts.createSourceFile("verify-windows-x64.mjs",
  readFileSync(new URL("./verify-windows-x64.mjs", import.meta.url), "utf8"),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const declaration = source.statements.find(node => ts.isFunctionDeclaration(node)
  && node.name?.text === "stopLauncher");

function cleanup({ commandError, exitCode, signalCode, exitAfterStop = true } = {}) {
  const child = new EventEmitter();
  child.exitCode = exitCode ?? null;
  child.signalCode = signalCode ?? null;
  child.kill = () => { throw new Error("验证器不得自行发送信号"); };
  const commands = [];
  const scheduled = [];
  const cleared = [];
  const options = { nodeExecutable: "C:/bundled node/node.exe", launcherEntry: "C:/relocated app/launcher/bin/boxteam.mjs",
    cwd: "C:/relocated app", env: { BOXTEAM_HOME: "C:/isolated home", PATH: "C:/empty" } };
  const context = vm.createContext({
    once,
    run(command, args, actualOptions) {
      commands.push({ command, args: Array.from(args), options: actualOptions });
      if (commandError) throw commandError;
      if (exitAfterStop) queueMicrotask(() => {
        child.exitCode = 0;
        child.emit("exit", 0, null);
      });
    },
    setTimeout(callback, delayMs) {
      const timer = { callback, delayMs };
      scheduled.push(timer);
      return timer;
    },
    clearTimeout(timer) { cleared.push(timer); },
  });
  vm.runInContext(declaration.getText(source), context);
  return { commands, scheduled, cleared, options, stop: () => context.stopLauncher(child, options) };
}

test("使用发行 Node、原 Launcher 与隔离环境执行 stop，并等待原进程退出", async () => {
  const run = cleanup();
  await run.stop();
  expect(run.commands).toEqual([{
    command: run.options.nodeExecutable,
    args: [run.options.launcherEntry, "stop"],
    options: { cwd: run.options.cwd, env: run.options.env, timeout: 180_000 },
  }]);
  expect(run.scheduled.map(timer => timer.delayMs)).toEqual([5_000]);
  expect(run.cleared).toEqual(run.scheduled);
});

for (const state of [{ exitCode: 7 }, { signalCode: "SIGTERM" }]) {
  test(`原 Launcher 已结束时不发送新的停机命令：${JSON.stringify(state)}`, async () => {
    const run = cleanup(state);
    await run.stop();
    expect(run.commands).toEqual([]);
    expect(run.scheduled).toEqual([]);
  });
}

test("stop 失败原样报告，不再自行强杀进程", async () => {
  const commandError = new Error("原认证控制通道拒绝停止");
  const run = cleanup({ commandError });
  await expect(run.stop()).rejects.toBe(commandError);
  expect(run.commands).toHaveLength(1);
  expect(run.scheduled).toEqual([]);
});

test("stop 命令完成但原 Launcher 未退出时明确失败并清理计时器", async () => {
  const run = cleanup({ exitAfterStop: false });
  const stopping = run.stop();
  run.scheduled[0].callback();
  await expect(stopping).rejects.toThrow("原 Launcher 仍未退出");
  expect(run.commands).toHaveLength(1);
  expect(run.cleared).toEqual(run.scheduled);
});

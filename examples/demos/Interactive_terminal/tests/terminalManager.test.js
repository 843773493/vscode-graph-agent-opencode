import { describe, expect, test } from "bun:test";
import { FIXED_TERMINAL_ID, TerminalManager } from "../src/server/terminalManager.js";

class FakePty {
  constructor(shell, args, options) {
    this.shell = shell;
    this.args = args;
    this.options = options;
    this.writes = [];
    this.resizes = [];
    this.killed = false;
    this.dataHandlers = new Set();
    this.exitHandlers = new Set();
  }

  onData(handler) {
    this.dataHandlers.add(handler);
    return { dispose: () => this.dataHandlers.delete(handler) };
  }

  onExit(handler) {
    this.exitHandlers.add(handler);
    return { dispose: () => this.exitHandlers.delete(handler) };
  }

  write(data) {
    this.writes.push(data);
  }

  resize(cols, rows) {
    this.resizes.push({ cols, rows });
  }

  kill() {
    this.killed = true;
    for (const handler of this.exitHandlers) {
      handler({ exitCode: 0, signal: 0 });
    }
  }

  emitData(data) {
    for (const handler of this.dataHandlers) {
      handler(data);
    }
  }
}

function createHarness() {
  const spawned = [];
  const manager = new TerminalManager({
    cwd: "/tmp/interactive-terminal-test",
    env: { SHELL: "/bin/test-sh" },
    shell: "/bin/test-sh",
    ptySpawn: (shell, args, options) => {
      const fakePty = new FakePty(shell, args, options);
      spawned.push(fakePty);
      return fakePty;
    },
  });
  return { manager, spawned };
}

describe("TerminalManager 原型", () => {
  test("attach uuid:12 后创建并复用同一个 PTY", () => {
    const { manager, spawned } = createHarness();
    const firstOutput = [];
    const secondOutput = [];

    const first = manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:1",
      onOutput: (data) => firstOutput.push(data),
      onExit: () => undefined,
    });
    const second = manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:2",
      onOutput: (data) => secondOutput.push(data),
      onExit: () => undefined,
    });

    expect(first.terminalId).toBe(FIXED_TERMINAL_ID);
    expect(second.terminalId).toBe(FIXED_TERMINAL_ID);
    expect(spawned).toHaveLength(1);

    spawned[0].emitData("hello");
    expect(firstOutput).toEqual(["hello"]);
    expect(secondOutput).toEqual(["hello"]);
  });

  test("detach 只断开客户端，不 kill PTY", () => {
    const { manager, spawned } = createHarness();
    const output = [];
    manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:1",
      onOutput: (data) => output.push(data),
      onExit: () => undefined,
    });

    manager.detach(FIXED_TERMINAL_ID, "client:1");
    spawned[0].emitData("after-detach");

    expect(output).toEqual([]);
    expect(spawned[0].killed).toBe(false);
  });

  test("用户输入和 agent 输入都写入同一个终端", () => {
    const { manager, spawned } = createHarness();
    manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:1",
      onOutput: () => undefined,
      onExit: () => undefined,
    });

    manager.write(FIXED_TERMINAL_ID, "pwd\n");
    manager.agentWrite(FIXED_TERMINAL_ID, "echo agent\n");

    expect(spawned[0].writes).toEqual(["pwd\n", "echo agent\n"]);
  });

  test("非法 terminalId 和缺失 data 会快速失败", () => {
    const { manager } = createHarness();
    expect(() =>
      manager.attach({
        terminalId: "",
        clientId: "client:1",
        onOutput: () => undefined,
        onExit: () => undefined,
      }),
    ).toThrow("terminalId 不能为空");

    manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:1",
      onOutput: () => undefined,
      onExit: () => undefined,
    });
    expect(() => manager.write(FIXED_TERMINAL_ID)).toThrow("data 必须是字符串");
  });
});

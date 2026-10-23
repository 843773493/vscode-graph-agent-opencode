import { describe, expect, test } from "bun:test";
import {
  FIXED_TERMINAL_ID,
  resolveDefaultShell,
  TerminalManager,
} from "../src/server/terminalManager.js";

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

  emitExit(event = { exitCode: 0, signal: 0 }) {
    for (const handler of this.exitHandlers) {
      handler(event);
    }
  }
}

function createHarness() {
  const spawned = [];
  const manager = new TerminalManager({
    cwd: process.cwd(),
    env: {},
    shell: "fixture-shell",
    ptySpawn: (shell, args, options) => {
      const fakePty = new FakePty(shell, args, options);
      spawned.push(fakePty);
      return fakePty;
    },
  });
  return { manager, spawned };
}

describe("TerminalManager 原型", () => {
  test("默认 shell 选择覆盖 Windows、Linux 和 macOS", () => {
    expect(resolveDefaultShell({ COMSPEC: "C:\\Windows\\System32\\cmd.exe" }, "win32"))
      .toBe("C:\\Windows\\System32\\cmd.exe");
    expect(resolveDefaultShell({}, "win32")).toBe("cmd.exe");
    expect(resolveDefaultShell({ SHELL: "/bin/zsh" }, "darwin")).toBe("/bin/zsh");
    expect(resolveDefaultShell({}, "darwin")).toBe("/bin/sh");
    expect(resolveDefaultShell({}, "linux")).toBe("/bin/sh");
  });

  test("attach uuid:12 后创建并复用同一个 PTY", () => {
    const { manager, spawned } = createHarness();
    const firstEvents = [];
    const secondEvents = [];

    const first = manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:1",
      onOutput: (outputEvent) => firstEvents.push(outputEvent),
      onExit: () => undefined,
    });
    const second = manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:2",
      onOutput: (outputEvent) => secondEvents.push(outputEvent),
      onExit: () => undefined,
    });

    expect(first.terminalId).toBe(FIXED_TERMINAL_ID);
    expect(second.terminalId).toBe(FIXED_TERMINAL_ID);
    expect(spawned).toHaveLength(1);

    spawned[0].emitData("hello");
    expect(firstEvents[0].terminalId).toBe(FIXED_TERMINAL_ID);
    expect(firstEvents[0].data).toBe("hello");
    expect(secondEvents[0].data).toBe("hello");
    expect(firstEvents[0]).toBe(secondEvents[0]);
    expect(Object.isFrozen(firstEvents[0])).toBe(true);
  });

  test("终端尺寸未变化时不重复调用 PTY resize", () => {
    const { manager, spawned } = createHarness();
    const attachResult = manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:1",
      onOutput: () => undefined,
      onExit: () => undefined,
      cols: 120,
      rows: 50,
    });

    manager.resize(FIXED_TERMINAL_ID, 120, 50);
    manager.resize(FIXED_TERMINAL_ID, 160, 40);
    manager.resize(FIXED_TERMINAL_ID, 160, 40);

    expect(attachResult).toMatchObject({ cols: 120, rows: 50 });
    expect(spawned[0].resizes).toEqual([{ cols: 160, rows: 40 }]);
  });

  test("PTY resize 失败时不登记客户端并允许用相同尺寸重试", () => {
    const { manager, spawned } = createHarness();
    const firstClientOutput = [];
    const retryClientOutput = [];
    manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:1",
      onOutput: (outputEvent) => firstClientOutput.push(outputEvent.data),
      onExit: () => undefined,
    });

    const resizeCalls = [];
    const resizeUnderlyingPty = spawned[0].resize.bind(spawned[0]);
    let failNextResize = true;
    spawned[0].resize = (cols, rows) => {
      resizeCalls.push({ cols, rows });
      if (failNextResize) {
        failNextResize = false;
        throw new Error("PTY resize failed");
      }
      resizeUnderlyingPty(cols, rows);
    };

    const attachRetryClient = () => manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:2",
      onOutput: (outputEvent) => retryClientOutput.push(outputEvent.data),
      onExit: () => undefined,
      cols: 120,
      rows: 50,
    });

    expect(attachRetryClient).toThrow("PTY resize failed");
    expect(manager.clientSessions.has("client:2")).toBe(false);
    spawned[0].emitData("after-failed-attach");
    expect(firstClientOutput).toEqual(["after-failed-attach"]);
    expect(retryClientOutput).toEqual([]);

    expect(attachRetryClient()).toMatchObject({ cols: 120, rows: 50 });
    expect(resizeCalls).toEqual([
      { cols: 120, rows: 50 },
      { cols: 120, rows: 50 },
    ]);
    expect(spawned[0].resizes).toEqual([{ cols: 120, rows: 50 }]);
    spawned[0].emitData("after-retry");
    expect(firstClientOutput).toEqual(["after-failed-attach", "after-retry"]);
    expect(retryClientOutput).toEqual(["after-retry"]);
  });

  test("detach 只断开客户端，不 kill PTY", () => {
    const { manager, spawned } = createHarness();
    const output = [];
    manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:1",
      onOutput: (outputEvent) => output.push(outputEvent.data),
      onExit: () => undefined,
    });

    manager.detach(FIXED_TERMINAL_ID, "client:1");
    spawned[0].emitData("after-detach");

    expect(output).toEqual([]);
    expect(spawned[0].killed).toBe(false);
  });

  test("服务关闭时 killAll 终止所有 PTY，后续 attach 可重建终端", () => {
    const { manager, spawned } = createHarness();
    const exits = [];
    manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:before-shutdown",
      onOutput: () => undefined,
      onExit: (event) => exits.push(event),
    });

    manager.killAll();
    manager.killAll();

    expect(spawned[0].killed).toBe(true);
    expect(exits).toEqual([{ exitCode: 0, signal: 0 }]);
    expect(manager.sessions.size).toBe(0);

    manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:after-shutdown",
      onOutput: () => undefined,
      onExit: () => undefined,
    });
    expect(spawned).toHaveLength(2);
  });

  test("PTY 退出后解除数据和退出订阅", () => {
    const { manager, spawned } = createHarness();
    const exits = [];
    manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:1",
      onOutput: () => undefined,
      onExit: (event) => exits.push(event),
    });
    const exitEvent = { exitCode: 0, signal: 0 };

    spawned[0].emitExit(exitEvent);
    spawned[0].emitData("after-exit");

    expect(exits).toEqual([exitEvent]);
    expect(spawned[0].dataHandlers.size).toBe(0);
    expect(spawned[0].exitHandlers.size).toBe(0);
  });

  test("旧 PTY 的延迟 detach 不会影响重建后的同 ID 终端", () => {
    const { manager, spawned } = createHarness();
    const newSessionOutput = [];
    manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:old",
      onOutput: () => undefined,
      onExit: () => undefined,
    });

    spawned[0].emitExit();
    manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:new",
      onOutput: (outputEvent) => newSessionOutput.push(outputEvent.data),
      onExit: () => undefined,
    });

    expect(spawned).toHaveLength(2);
    manager.detach(FIXED_TERMINAL_ID, "client:old");
    spawned[1].emitData("new-session");

    expect(newSessionOutput).toEqual(["new-session"]);
  });

  test("多个输入依次写入同一个终端 PTY", () => {
    const { manager, spawned } = createHarness();
    manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:1",
      onOutput: () => undefined,
      onExit: () => undefined,
    });

    manager.write(FIXED_TERMINAL_ID, "pwd\n");
    manager.write(FIXED_TERMINAL_ID, "echo agent\n");

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

  test("非法 attach 参数在创建 PTY 之前失败", () => {
    const { manager, spawned } = createHarness();
    const onOutput = () => undefined;
    const onExit = () => undefined;

    expect(() => manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "",
      onOutput,
      onExit,
    })).toThrow("clientId 不能为空");
    expect(() => manager.attach({
      terminalId: FIXED_TERMINAL_ID,
      clientId: "client:1",
      onOutput,
    })).toThrow("onExit 必须是函数");
    expect(spawned).toHaveLength(0);
  });
});

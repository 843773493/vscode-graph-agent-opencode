import { describe, expect, test } from "bun:test";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { once } from "node:events";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import path from "node:path";
import vm from "node:vm";
import ts from "typescript";

import { remainingOccupiedPorts } from "./dev-environment.mjs";

const runId = randomUUID();
const testOutputRoot = path.resolve("out/tests/scripts/launch/dev-port-listeners");
const fixtureProjectRoot = path.join(testOutputRoot, "workspace", runId);
const artifactDirectory = path.join(testOutputRoot, "artifacts", runId);
const posixHarnessPlatform = process.platform === "win32" ? "linux" : process.platform;
for (const kind of ["workspace", "artifacts"]) {
  mkdirSync(path.join(testOutputRoot, kind), { recursive: true });
  mkdirSync(path.join(testOutputRoot, kind, runId));
}

const devManagerSource = readFileSync(path.resolve("scripts/launch/dev.mjs"), "utf8");
const devManagerAst = ts.createSourceFile("dev.mjs", devManagerSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const portCleanupSource = ["readUnixListenerPidsByPort", "assertDevelopmentPortsFree", "cleanDevelopmentPorts"].map((name) => {
  const node = devManagerAst.statements.find((entry) => ts.isFunctionDeclaration(entry) && entry.name?.text === name);
  if (!node) throw new Error(`dev.mjs 缺少 ${name} 入口`);
  return node.getText(devManagerAst);
}).join("\n");

function createPortCleanupHarness(spawnSync, {
  ports = [23947],
  kill = () => { throw new Error("此用例禁止发送进程信号"); },
  sleep = () => { throw new Error("错误路径不应进入终止后等待"); },
} = {}) {
  // 不加载 dev.mjs 顶层编排；默认拒绝信号，真实清理用例只授权自己的子进程。
  return vm.runInNewContext(`${portCleanupSource}\n({ readUnixListenerPidsByPort, cleanDevelopmentPorts })`, {
    projectRoot: fixtureProjectRoot,
    process: { platform: posixHarnessPlatform, kill },
    TextDecoder,
    selectedPorts: () => ports,
    remainingOccupiedPorts,
    Bun: { spawnSync, sleep },
  });
}

function saveObservation(scenario, result) {
  writeFileSync(path.join(artifactDirectory, `${scenario}.json`), `${JSON.stringify(result, null, 2)}\n`, { flag: "wx" });
}

describe("Unix 开发端口监听者选择", () => {
  test.skipIf(process.platform === "win32" || Bun.which("lsof") === null)("真实已占用和空闲多端口只查询一次，不把客户端列入清理名单", async () => {
    const servers = [createServer(), createServer(), createServer()];
    const sockets = new Set();
    for (const server of servers) server.on("connection", (socket) => sockets.add(socket));
    let client, closed;
    const observation = { listenerPid: process.pid, ports: [], commands: [] };
    try {
      for (const server of servers) {
        const listening = once(server, "listening");
        server.listen(0, "127.0.0.1");
        await listening;
        const address = server.address();
        if (address === null || typeof address === "string") throw new Error("未取得 TCP fixture 端口");
        observation.ports.push(address.port);
      }
      await new Promise((resolve, reject) => servers[2].close((error) => error ? reject(error) : resolve()));
      client = spawn(process.execPath, [
        "-e",
        'import { createConnection } from "node:net"; createConnection({ host: "127.0.0.1", port: Number(process.argv[1]) }).once("connect", () => process.stdout.write("connected\\n"));',
        String(observation.ports[0]),
      ], { cwd: fixtureProjectRoot, stdio: ["ignore", "pipe", "pipe"] });
      closed = new Promise((resolve) => client.once("close", (code, signal) => resolve([code, signal])));
      await once(client, "spawn");
      observation.clientPid = client.pid;
      await Promise.race([
        once(client.stdout, "data", { signal: AbortSignal.timeout(1500) }),
        closed.then(([code, signal]) => { throw new Error(`TCP 客户端提前退出: exit=${code} signal=${signal}`); }),
      ]);
      const portCleanup = createPortCleanupHarness((command, options) => {
        if (command[0] !== "lsof") throw new Error(`禁止 fixture 执行 ${command[0]}`);
        const result = Bun.spawnSync(command, options);
        observation.commands.push({ command, exitCode: result.exitCode, signalCode: result.signalCode,
          stdout: result.stdout.toString(), stderr: result.stderr?.toString() ?? null });
        return result;
      });
      const listeners = portCleanup.readUnixListenerPidsByPort(observation.ports);
      observation.detectedPidsByPort = Object.fromEntries(listeners);
      for (const [index, port] of observation.ports.entries()) {
        expect([...listeners.get(port)]).toEqual(index < 2 ? [String(process.pid)] : []);
        expect([...listeners.get(port)]).not.toContain(String(client.pid));
      }
      expect(observation.commands).toHaveLength(1);
    } finally {
      if (client?.pid && client.exitCode === null && client.signalCode === null) client.kill("SIGTERM");
      for (const socket of sockets) socket.destroy();
      await closed;
      await Promise.all(servers.filter((server) => server.listening).map((server) =>
        new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))));
      saveObservation("native-listener-and-client", observation);
    }
  }, 5000);

  test.skipIf(process.platform === "win32" || Bun.which("lsof") === null)("进程API清理真实监听者，不依赖外部kill命令", async () => {
    let child, closed;
    const observation = { commands: [], signals: [], stdout: "", stderr: "" };
    try {
      child = spawn(process.execPath, ["-e",
        'import { createServer } from "node:net"; const server = createServer(); server.listen(0, "127.0.0.1", () => console.log(server.address().port));',
      ], { cwd: fixtureProjectRoot, stdio: ["ignore", "pipe", "pipe"], timeout: 4500, killSignal: "SIGKILL" });
      closed = new Promise((resolve) => child.once("close", (code, signal) => resolve({ code, signal })));
      observation.pid = child.pid;
      const port = await new Promise((resolve, reject) => {
        child.once("error", reject);
        child.once("close", (code, signal) => reject(new Error(`监听fixture提前退出: exit=${code} signal=${signal}`)));
        child.stderr.setEncoding("utf8").on("data", (chunk) => { observation.stderr += chunk; });
        child.stdout.setEncoding("utf8").on("data", (chunk) => {
          observation.stdout += chunk;
          if (observation.stdout.includes("\n")) resolve(Number(observation.stdout.split("\n")[0]));
        });
      });
      expect(Number.isInteger(port) && port > 0 && port <= 65535).toBe(true);
      observation.port = port;
      const portCleanup = createPortCleanupHarness((command, options) => {
        observation.commands.push(command);
        if (command[0] !== "lsof") throw new Error(`禁止 fixture 执行 ${command[0]}`);
        return Bun.spawnSync(command, options);
      }, {
        ports: [port],
        kill(pid, signal) {
          if (pid !== child.pid) throw new Error(`禁止向非fixture进程发信号: ${pid}`);
          observation.signals.push([pid, signal]);
          return process.kill(pid, signal);
        },
        sleep: Bun.sleep,
      });
      await portCleanup.cleanDevelopmentPorts();
      observation.exit = await closed;
      expect(observation.signals).toEqual([[child.pid, "SIGTERM"]]);
      expect(observation.exit.signal).toBe("SIGTERM");
      expect(observation.commands.every((command) => command[0] === "lsof")).toBe(true);
    } finally {
      if (child?.pid && child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
      observation.exit ??= await closed;
      saveObservation("native-process-api-cleanup", observation);
    }
  }, 8000);

  test("进程API在TERM后重查并只KILL残留监听者，最后确认释放", async () => {
    const events = [];
    let queries = 0;
    const portCleanup = createPortCleanupHarness((command) => {
      if (command[0] !== "lsof") throw new Error(`禁止 fixture 执行 ${command[0]}`);
      events.push("query");
      queries++;
      return { exitCode: queries < 3 ? 0 : 1, signalCode: null,
        stdout: Buffer.from(queries < 3 ? "p41001\nn127.0.0.1:23947\n" : ""), stderr: Buffer.from("") };
    }, {
      kill(pid, signal) { events.push([pid, signal]); },
      async sleep(milliseconds) { events.push(["sleep", milliseconds]); },
    });
    await portCleanup.cleanDevelopmentPorts();
    saveObservation("process-api-force-order", { events });
    expect(events).toEqual([
      "query", [41001, "SIGTERM"], ["sleep", 1000],
      "query", [41001, "SIGKILL"], "query",
    ]);
  });

  test("七端口只查询三次，各阶段对同一监听PID去重，保留TERM等待及最终确认", async () => {
    const ports = [23947, 23948, 23949, 23950, 23951, 23952, 23953];
    const calls = [], events = [];
    const outputs = [
      "p41001\nn*:23947\nn[::]:23948\np41002\nn127.0.0.1:23949\n",
      "p41001\nn*:23947\nn[::]:23948\n",
      "",
    ];
    const portCleanup = createPortCleanupHarness((command) => {
      calls.push([...command]);
      if (calls.length > 3) throw new Error("发生多余的lsof查询");
      events.push("query");
      return { exitCode: calls.length < 3 ? 0 : 1, signalCode: null,
        stdout: Buffer.from(outputs[calls.length - 1]), stderr: Buffer.from("") };
    }, {
      ports,
      kill(pid, signal) { events.push([pid, signal]); },
      async sleep(milliseconds) { events.push(["sleep", milliseconds]); },
    });
    await portCleanup.cleanDevelopmentPorts();
    saveObservation("three-multiport-snapshots", { ports, calls, events });
    expect(calls).toEqual(Array.from({ length: 3 }, () => [
      "lsof", "-nP", "-a", `-iTCP:${ports.join(",")}`, "-sTCP:LISTEN", "-Fpn",
    ]));
    expect(events).toEqual([
      "query", [41001, "SIGTERM"], [41002, "SIGTERM"], ["sleep", 1000],
      "query", [41001, "SIGKILL"], "query",
    ]);
  });

  for (const errorCode of ["ESRCH", "EPERM"]) {
    test(`进程API保留${errorCode}的实际含义，不静默吞掉权限失败`, async () => {
      const failure = Object.assign(new Error(`fixture signal ${errorCode}`), { code: errorCode });
      const calls = [], signals = [], delays = [];
      const portCleanup = createPortCleanupHarness((command) => {
        calls.push(command);
        if (command[0] !== "lsof") throw new Error(`禁止 fixture 执行 ${command[0]}`);
        return { exitCode: calls.length === 1 ? 0 : 1, signalCode: null,
          stdout: Buffer.from(calls.length === 1 ? "p41001\nn127.0.0.1:23947\n" : ""), stderr: Buffer.from("") };
      }, {
        kill(pid, signal) { signals.push([pid, signal]); throw failure; },
        async sleep(milliseconds) { delays.push(milliseconds); },
      });
      let observedError;
      try { await portCleanup.cleanDevelopmentPorts(); }
      catch (error) { observedError = error; }
      saveObservation(`process-api-${errorCode}`, { calls, signals, delays, error: observedError?.message });
      expect(signals).toEqual([[41001, "SIGTERM"]]);
      if (errorCode === "ESRCH") {
        expect(observedError).toBeUndefined();
        expect(delays).toEqual([1000]);
        expect(calls).toHaveLength(3);
      } else {
        expect(observedError).toBe(failure);
        expect(delays).toEqual([]);
        expect(calls).toHaveLength(1);
      }
    });
  }

  test("lsof 明确无匹配时允许端口空闲", () => {
    const calls = [];
    const portCleanup = createPortCleanupHarness((command) => {
      calls.push(command);
      return { exitCode: 1, signalCode: null, stdout: Buffer.from(""), stderr: Buffer.from("") };
    });
    const pids = [...portCleanup.readUnixListenerPidsByPort([23947]).get(23947)];
    saveObservation("no-listener", { calls, pids });
    expect(pids).toEqual([]);
    expect(calls).toHaveLength(1);
  });

  for (const stdout of ["", "pnot-a-pid", "p123\ninvalid", "p0\nn*:23947",
    "n*:23947", "p123", "p123\nn*:23948", "p123\nninvalid"]) {
    test(`成功查询也不能用无效监听记录 ${JSON.stringify(stdout)} 伪报空闲`, () => {
      const portCleanup = createPortCleanupHarness(() => ({
        exitCode: 0, signalCode: null, stdout: Buffer.from(stdout), stderr: Buffer.from(""),
      }));
      expect(() => portCleanup.readUnixListenerPidsByPort([23947])).toThrow(/无效监听/);
    });
  }

  test("同一PID的IPv4和IPv6监听在端口内去重", () => {
    const portCleanup = createPortCleanupHarness(() => ({ exitCode: 0, signalCode: null,
      stdout: Buffer.from("p41001\nn*:23947\nn[::]:23947\np41002\nn127.0.0.1:23947\n"), stderr: Buffer.from("") }));
    expect([...portCleanup.readUnixListenerPidsByPort([23947]).get(23947)]).toEqual(["41001", "41002"]);
  });

  for (const failure of [
    { name: "command-error", exitCode: 2, signalCode: null, stderr: "lsof fixture command failed" },
    { name: "warning-with-no-match", exitCode: 1, signalCode: null, stderr: "lsof fixture permission denied" },
    { name: "signal", exitCode: null, signalCode: 15, stderr: "lsof fixture interrupted" },
  ]) {
    test(`${failure.name} 明确失败，不能当作空闲或继续发信号`, async () => {
      const calls = [];
      const portCleanup = createPortCleanupHarness((command) => {
        calls.push(command);
        if (command[0] !== "lsof") throw new Error(`禁止 fixture 执行 ${command[0]}`);
        return { ...failure, stdout: Buffer.from(""), stderr: Buffer.from(failure.stderr) };
      });
      let observedError;
      try {
        await portCleanup.cleanDevelopmentPorts();
      } catch (error) {
        observedError = error;
      }
      saveObservation(failure.name, { calls, error: observedError?.message });
      expect(observedError).toBeDefined();
      expect(observedError?.message).toContain(failure.stderr);
      expect(observedError?.message).toContain("23947");
      expect(observedError?.message).toContain(`exit=${failure.exitCode}`);
      expect(observedError?.message).toContain(`signal=${failure.signalCode ?? "none"}`);
      expect(calls).toHaveLength(1);
    });
  }
});

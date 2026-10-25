import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import ts from "typescript";

import { remainingOccupiedPorts, resolveDevelopmentLayout } from "./dev-environment.mjs";

const runId = randomUUID();
const testOutputRoot = path.resolve("out/tests/scripts/launch/dev-windows-port-listeners");
for (const kind of ["workspace", "artifacts"]) {
  mkdirSync(path.join(testOutputRoot, kind), { recursive: true });
  mkdirSync(path.join(testOutputRoot, kind, runId));
}
const fixtureProjectRoot = path.join(testOutputRoot, "workspace", runId);
const artifactDirectory = path.join(testOutputRoot, "artifacts", runId);
const developmentPorts = Object.values(resolveDevelopmentLayout({
  environment: {},
  cwd: fixtureProjectRoot,
}).ports);
const netstatCommand = ["netstat", "-ano", "-p", "tcp"];
const sampleNetstatOutput = [
  "Active Connections",
  "  Proto  Local Address          Foreign Address        State           PID",
  "  TCP    127.0.0.1:8010         0.0.0.0:0              LISTENING       41001",
  "  TCP    [::]:8010              [::]:0                 LISTENING       41001",
  "  TCP    127.0.0.1:8012         0.0.0.0:0              LISTENING       41003",
  "  TCP    [::1]:8014             [::]:0                 LISTENING       41002",
  "  TCP    127.0.0.1:8015         0.0.0.0:0              LISTENING       41004",
  "  TCP    127.0.0.1:8002         0.0.0.0:0              LISTENING       41005",
  "  TCP    127.0.0.1:8011         127.0.0.1:9030         ESTABLISHED     51001",
  "  TCP    127.0.0.1:9030         127.0.0.1:8014         ESTABLISHED     51002",
  "  TCP    127.0.0.1:18010        0.0.0.0:0              LISTENING       51003",
  "  TCP    127.0.0.1:8012         0.0.0.0:0              NOT_LISTENING   51004",
  "  UDP    127.0.0.1:8013         *:*                    LISTENING       51005",
].join("\r\n");
const emptyNetstatOutput = "Active Connections\r\n  Proto  Local Address  Foreign Address  State  PID\r\n";

const devManagerSource = readFileSync(path.resolve("scripts/launch/dev.mjs"), "utf8");
const devManagerAst = ts.createSourceFile("dev.mjs", devManagerSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const portCleanupSource = ["readWindowsListenerPidsByPort", "assertDevelopmentPortsFree", "cleanDevelopmentPorts"].map((name) => {
  const node = devManagerAst.statements.find((entry) => ts.isFunctionDeclaration(entry) && entry.name?.text === name);
  if (!node) throw new Error(`dev.mjs 缺少 ${name} 入口`);
  return node.getText(devManagerAst);
}).join("\n");

function successfulNetstatQuery(stdout) {
  return { exitCode: 0, signalCode: null, stdout: Buffer.from(stdout), stderr: Buffer.from("") };
}

async function observeWindowsPortCleanup(scenario, netstatResponses, taskkillExitCode = 0) {
  const calls = [], queryResults = [];
  let queryIndex = 0, cleanupError;
  // 仅执行从真实源码提取的 Windows 分支；所有命令均是替身，禁止操作用户进程。
  const cleanDevelopmentPorts = vm.runInNewContext(`${portCleanupSource}\ncleanDevelopmentPorts`, {
    projectRoot: fixtureProjectRoot,
    process: { platform: "win32" },
    TextDecoder,
    selectedPorts: () => developmentPorts,
    remainingOccupiedPorts,
    Bun: { spawnSync(command, options) {
      calls.push([...command]);
      if (options.cwd !== fixtureProjectRoot) throw new Error("fixture cwd 越界");
      if (command[0] === "netstat") {
        if (JSON.stringify(command) !== JSON.stringify(netstatCommand)) throw new Error("netstat 命令不符合合同");
        const result = netstatResponses[queryIndex++];
        if (!result) throw new Error("发生多余的全表查询");
        queryResults.push({ exitCode: result.exitCode, signalCode: result.signalCode,
          stdout: result.stdout.toString(), stderr: result.stderr.toString() });
        return result;
      }
      if (command[0] !== "taskkill" || options.stdout !== "ignore" || options.stderr !== "pipe") {
        throw new Error(`fixture 禁止执行命令: ${command.join(" ")}`);
      }
      return { exitCode: taskkillExitCode };
    } },
  });
  try { await cleanDevelopmentPorts(); }
  catch (error) { cleanupError = error; }
  writeFileSync(path.join(artifactDirectory, `${scenario}.json`), `${JSON.stringify({
    developmentPorts, calls, queryResults, error: cleanupError?.message,
  }, null, 2)}\n`, { flag: "wx" });
  return { calls, cleanupError };
}

describe("Windows 开发端口清理的分阶段快照", () => {
  test("实际开发端口和调试端口只查询两次，IPv4和IPv6重复PID只清理一次", async () => {
    const { calls, cleanupError } = await observeWindowsPortCleanup("two-snapshots", [
      successfulNetstatQuery(sampleNetstatOutput), successfulNetstatQuery(emptyNetstatOutput),
    ]);
    expect(cleanupError).toBeUndefined();
    expect(calls).toEqual([
      netstatCommand,
      ["taskkill", "/T", "/F", "/PID", "41001"],
      ["taskkill", "/T", "/F", "/PID", "41003"],
      ["taskkill", "/T", "/F", "/PID", "41002"],
      ["taskkill", "/T", "/F", "/PID", "41004"],
      ["taskkill", "/T", "/F", "/PID", "41005"],
      netstatCommand,
    ]);
  });

  test("清理后的快照仍有新监听者时明确拒绝启动，不复用旧结果", async () => {
    const { calls, cleanupError } = await observeWindowsPortCleanup("replacement-listener", [
      successfulNetstatQuery(sampleNetstatOutput),
      successfulNetstatQuery("  TCP    [::]:8014    [::]:0    LISTENING    41003\r\n"),
    ]);
    expect(cleanupError?.message).toContain("开发端口清理后仍被占用");
    expect(cleanupError?.message).toContain("8014(pid=41003)");
    expect(calls.at(-1)).toEqual(netstatCommand);
    expect(calls.filter(([command]) => command === "netstat")).toHaveLength(2);
  });

  test("空闲端口也要重新确认，但不发送终止命令", async () => {
    const { calls, cleanupError } = await observeWindowsPortCleanup("no-listeners", [
      successfulNetstatQuery(emptyNetstatOutput), successfulNetstatQuery(emptyNetstatOutput),
    ]);
    expect(cleanupError).toBeUndefined();
    expect(calls).toEqual([netstatCommand, netstatCommand]);
  });

  for (const queryFailure of [
    { name: "query-error", exitCode: 2, signalCode: null, stderr: "fixture netstat permission denied" },
    { name: "query-signal", exitCode: null, signalCode: 15, stderr: "fixture netstat interrupted" },
    { name: "signal-with-zero-exit", exitCode: 0, signalCode: 15, stderr: "fixture netstat interrupted" },
  ]) {
    test(`${queryFailure.name} 保留命令和原始原因，不继续清理`, async () => {
      const { calls, cleanupError } = await observeWindowsPortCleanup(queryFailure.name, [{
        ...queryFailure, stdout: Buffer.from(sampleNetstatOutput), stderr: Buffer.from(queryFailure.stderr),
      }]);
      expect(cleanupError?.message).toContain("netstat -ano -p tcp");
      expect(cleanupError?.message).toContain(`exit=${queryFailure.exitCode}`);
      expect(cleanupError?.message).toContain(`signal=${queryFailure.signalCode ?? "none"}`);
      expect(cleanupError?.message).toContain(queryFailure.stderr);
      expect(calls).toEqual([netstatCommand]);
    });
  }

  for (const pid of ["0", "invalid"]) {
    test(`目标监听行含无效PID ${pid} 时禁止终止任何进程`, async () => {
      const { calls, cleanupError } = await observeWindowsPortCleanup(`invalid-pid-${pid}`, [successfulNetstatQuery(
        `  TCP    127.0.0.1:8010    0.0.0.0:0    LISTENING    ${pid}\r\n`,
      )]);
      expect(cleanupError?.message).toContain("无效 PID");
      expect(cleanupError?.message).toContain("port=8010");
      expect(calls).toEqual([netstatCommand]);
    });
  }

  test("最终查询失败不能伪报端口已经释放", async () => {
    const { calls, cleanupError } = await observeWindowsPortCleanup("confirmation-error", [
      successfulNetstatQuery(emptyNetstatOutput),
      { exitCode: 3, signalCode: null, stdout: Buffer.from(""), stderr: Buffer.from("fixture final query failed") },
    ]);
    expect(cleanupError?.message).toContain("fixture final query failed");
    expect(calls).toEqual([netstatCommand, netstatCommand]);
  });

  test("终止失败后立即退出，不以新的快照掩盖失败", async () => {
    const { calls, cleanupError } = await observeWindowsPortCleanup("taskkill-error", [successfulNetstatQuery(sampleNetstatOutput)], 1);
    expect(cleanupError?.message).toContain("无法清理 Windows 开发进程: pid=41001");
    expect(calls).toEqual([netstatCommand, ["taskkill", "/T", "/F", "/PID", "41001"]]);
  });
});

import { afterAll, describe, expect, spyOn, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  chmodSync,
  mkdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";

import {
  loadTargetConfiguration,
  selectTarget,
} from "../../tools/cross-platform-development-targets/config.mjs";
import { parseTargetCliArgs } from "../../tools/cross-platform-development-targets/cli-options.mjs";
import {
  assertCleanInitializedSubmodules,
  createWorkspaceSnapshot,
} from "../../tools/cross-platform-development-targets/snapshot.mjs";
import {
  runChecked,
  runCheckedWithInput,
} from "../../tools/cross-platform-development-targets/process.mjs";
import {
  assertMatchingSha256,
  sshCommand,
} from "../../tools/cross-platform-development-targets/ssh-target.mjs";

const projectRoot = process.cwd();
const fixtureRunRoot = path.resolve(
  "out/tests/scripts/launch/cross-platform-development-target/workspace", randomUUID(),
);
afterAll(() => rmSync(fixtureRunRoot, { recursive: true, force: true }));

function createFixtureRoot() {
  const root = path.join(fixtureRunRoot, `${randomUUID()}-目标 fixture`);
  mkdirSync(root, { recursive: true });
  return root;
}

function command(cwd, args) {
  const result = Bun.spawnSync(args, { cwd, stdout: "pipe", stderr: "pipe" });
  if (result.exitCode !== 0) {
    throw new Error(new TextDecoder().decode(result.stderr));
  }
  return new TextDecoder().decode(result.stdout).trim();
}

function initializeRepository(root) {
  command(root, ["git", "init", "-q"]);
  command(root, ["git", "config", "user.name", "Target Test"]);
  command(root, ["git", "config", "user.email", "target-test@localhost"]);
  writeFileSync(path.join(root, ".gitignore"), ".env\nout/\n", "utf8");
  writeFileSync(path.join(root, "tracked.txt"), "before\n", "utf8");
  writeFileSync(path.join(root, "deleted.txt"), "remove\n", "utf8");
  command(root, ["git", "add", "."]);
  command(root, ["git", "commit", "-qm", "initial"]);
}

async function observeShellCommand(entryUrl, platform, passthrough, identityFile, knownHostsFile, projectRoot) {
  const fs = await import("node:fs");
  const { default: path } = await import("node:path");
  const { createContext, SourceTextModule, SyntheticModule } = await import("node:vm");
  const cliOptions = await import(new URL("../../tools/cross-platform-development-targets/cli-options.mjs", entryUrl));
  const sshTarget = await import(new URL("../../tools/cross-platform-development-targets/ssh-target.mjs", entryUrl));
  const target = {
    id: `${platform}-shell-fixture`,
    platform,
    ssh: {
      host: `${platform}.example.invalid`,
      port: 22222,
      user: "boxteam",
      identityFile,
      knownHostsFile,
    },
  };
  const commands = [];
  const context = createContext({
    Buffer,
    process: {
      argv: [],
      env: {},
      cwd: () => projectRoot,
      stdout: { write() {} },
      stderr: { write() {} },
      exitCode: 0,
    },
  });
  const module = new SourceTextModule(fs.readFileSync(new URL(entryUrl), "utf8"), {
    identifier: entryUrl,
    context,
    initializeImportMeta(meta) { meta.main = false; },
  });
  const dependencies = new Map([
    ["node:fs", { mkdirSync: fs.mkdirSync }],
    ["node:path", { default: path }],
    ["../../tools/cross-platform-development-targets/cli-options.mjs", cliOptions],
    ["../../tools/cross-platform-development-targets/config.mjs", {
      loadTargetConfiguration: () => ({ projectRoot, targets: [target] }),
      selectTarget: (configuration, targetId) => configuration.targets.find((item) => item.id === targetId),
    }],
    ["../../tools/cross-platform-development-targets/docker-target.mjs", {
      provisionDockerTarget() { throw new Error("shell 测试不应启动 Docker"); },
    }],
    ["../../tools/cross-platform-development-targets/process.mjs", {
      runChecked(command) { commands.push(Array.from(command)); },
    }],
    ["../../tools/cross-platform-development-targets/snapshot.mjs", {
      createWorkspaceSnapshot() { throw new Error("shell 测试不应创建 Git 快照"); },
    }],
    ["../../tools/cross-platform-development-targets/ssh-target.mjs", sshTarget],
  ]);
  await module.link((specifier) => {
    const exports = dependencies.get(specifier);
    if (!exports) throw new Error(`未预期的 shell 入口依赖: ${specifier}`);
    return new SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
  });
  await module.evaluate();
  const args = ["shell", target.id, ...(passthrough.length > 0 ? ["--", ...passthrough] : [])];
  await module.namespace.runTargetCommand(args, { projectRoot });
  if (commands.length !== 1) throw new Error(`预期一个 SSH 调用，实际 ${commands.length}`);
  return {
    command: commands[0],
    destination: `${target.ssh.user}@${target.ssh.host}`,
    identityFile,
    knownHostsFile,
  };
}

function invokeShellCommand(platform, passthrough = []) {
  const root = createFixtureRoot();
  const identityFile = path.join(root, "fixture identity");
  const knownHostsFile = path.join(root, "fixture known hosts");
  writeFileSync(identityFile, "fixture identity only", "utf8");
  const entryUrl = new URL("./cross-platform-development-target.mjs", import.meta.url).href;
  const run = spawnSync("node", [
    "--experimental-vm-modules",
    "--input-type=module",
    "--eval",
    `${observeShellCommand.toString()}\nconsole.log(JSON.stringify(await observeShellCommand(...JSON.parse(process.argv[1]))));`,
    JSON.stringify([entryUrl, platform, passthrough, identityFile, knownHostsFile, projectRoot]),
  ], { cwd: root, encoding: "utf8", timeout: 5000 });
  if (run.error || run.status !== 0) {
    throw new Error(run.stderr || `shell 入口探针退出: ${run.status}`, { cause: run.error });
  }
  return JSON.parse(run.stdout);
}

async function observeEnvironmentUpload(entryUrl, projectRoot, platform, identityFile, knownHostsFile, localHash, failures) {
  const fs = await import("node:fs");
  const { createContext, SourceTextModule, SyntheticModule } = await import("node:vm");
  const crypto = await import("node:crypto");
  const path = await import("node:path");
  const processModule = await import("node:process");
  const processHelpers = await import(new URL("../../tools/cross-platform-development-targets/process.mjs", entryUrl));
  const calls = [];
  const target = {
    id: `${platform}-environment-fixture`,
    platform,
    paths: { repository: path.join(projectRoot, "remote repository") },
    ssh: {
      host: `${platform}.example.invalid`,
      port: 22222,
      user: "boxteam",
      identityFile,
      knownHostsFile,
    },
  };
  const context = createContext({ Buffer, process: processModule.default });
  const module = new SourceTextModule(fs.readFileSync(new URL(entryUrl), "utf8"), {
    identifier: entryUrl,
    context,
    initializeImportMeta(meta) { meta.main = false; },
  });
  const dependencies = new Map([
    ["node:crypto", { createHash: crypto.createHash }],
    ["node:fs", {
      chmodSync: fs.chmodSync,
      existsSync: fs.existsSync,
      readFileSync: fs.readFileSync,
      statSync: fs.statSync,
    }],
    ["node:path", { default: path.default }],
    ["node:process", { default: processModule.default }],
    ["./process.mjs", {
      ...processHelpers,
      runChecked(command, options) {
        calls.push({ kind: "scp", command: Array.from(command), label: options.label });
        if (failures.scp) throw new Error(failures.scp);
        return { exitCode: 0, stdout: "", stderr: "" };
      },
      async runCheckedWithInput(command, input, options) {
        let action;
        let args;
        if (platform === "linux") {
          const tokens = [...command.at(-1).matchAll(/'([^']*)'/g)].map((match) => match[1]);
          if (tokens.slice(0, 3).join(" ") !== "sh -s --") {
            throw new Error(`Linux 动作参数结构异常: ${command.at(-1)}`);
          }
          [action, ...args] = tokens.slice(3);
          if (!input.includes("set -eu")) throw new Error("未透传实际 Linux 管理脚本");
        } else {
          const script = Buffer.from(input.trim(), "base64").toString("utf8");
          const match = script.match(/BOXTEAM_TARGET_ARGUMENTS_BASE64='([^']+)'/);
          if (!match) throw new Error("未透传 Windows 结构化动作参数");
          [action, ...args] = JSON.parse(Buffer.from(match[1], "base64").toString("utf8"));
          if (!script.includes('"remove-upload" {')) throw new Error("未透传实际 Windows 管理脚本");
        }
        calls.push({ kind: "action", action, args, label: options.label });
        if (failures[action]) throw new Error(failures[action]);
        return {
          exitCode: 0,
          stdout: action === "hash-file" ? (failures.remoteHash ?? localHash) : "",
          stderr: "",
        };
      },
    }],
  ]);
  await module.link((specifier) => {
    const exports = dependencies.get(specifier);
    if (!exports) throw new Error(`未预期的 .env 同步依赖: ${specifier}`);
    return new SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
  });
  await module.evaluate();
  try {
    return {
      calls,
      result: await module.namespace.synchronizeEnvironmentFile({
        target,
        projectRoot,
      }),
      error: null,
    };
  } catch (error) {
    return {
      calls,
      result: null,
      error: {
        name: error?.name ?? typeof error,
        message: String(error?.message ?? error),
        cause: error?.cause ? {
          name: error.cause?.name ?? typeof error.cause,
          message: String(error.cause?.message ?? error.cause),
        } : null,
        errors: Array.isArray(error?.errors)
          ? error.errors.map((item) => ({
            name: item?.name ?? typeof item,
            message: String(item?.message ?? item),
          }))
          : null,
      },
    };
  }
}

function invokeEnvironmentUpload(platform, failures = {}) {
  const root = createFixtureRoot();
  const envPath = path.join(root, ".env");
  const identityFile = path.join(root, "fixture identity");
  const knownHostsFile = path.join(root, "fixture known hosts");
  const contents = "TOKEN=fixture-only\n";
  writeFileSync(envPath, contents, "utf8");
  writeFileSync(identityFile, "fixture identity only", "utf8");
  chmodSync(identityFile, 0o600);
  const actionScriptRelative = platform === "linux"
    ? "tools/cross-platform-development-targets/linux/manage-target.sh"
    : "tools/cross-platform-development-targets/windows/Manage-Target.ps1";
  const actionScriptPath = path.join(root, actionScriptRelative);
  mkdirSync(path.dirname(actionScriptPath), { recursive: true });
  writeFileSync(
    actionScriptPath,
    readFileSync(path.join(projectRoot, actionScriptRelative), "utf8"),
    "utf8",
  );
  const entryUrl = new URL("../../tools/cross-platform-development-targets/ssh-target.mjs", import.meta.url).href;
  const localHash = createHash("sha256").update(contents).digest("hex");
  const run = spawnSync("node", [
    "--experimental-vm-modules",
    "--input-type=module",
    "--eval",
    `${observeEnvironmentUpload.toString()}\nconsole.log(JSON.stringify(await observeEnvironmentUpload(...JSON.parse(process.argv[1]))));`,
    JSON.stringify([entryUrl, root, platform, identityFile, knownHostsFile, localHash, failures]),
  ], { cwd: root, encoding: "utf8", timeout: 5000 });
  if (run.error || run.status !== 0) {
    throw new Error(run.stderr || `.env 同步探针退出: ${run.status}`, { cause: run.error });
  }
  return JSON.parse(run.stdout);
}

async function observeDockerProvision(
  entryUrl,
  fixtureRoot,
  plan,
  existingKnownHosts,
  failKnownHostUpdate = false,
  removedKnownHostLines = [],
) {
  const fs = await import("node:fs");
  const path = await import("node:path");
  const { createContext, SourceTextModule, SyntheticModule } = await import("node:vm");
  const clock = { now: 0 };
  const childCalls = [];
  const sleeps = [];
  const checkedCommands = [];
  const knownHostsPath = path.join(fixtureRoot, "ssh", "known_hosts");
  if (existingKnownHosts !== null) {
    fs.mkdirSync(path.dirname(knownHostsPath), { recursive: true });
    fs.writeFileSync(knownHostsPath, existingKnownHosts, "utf8");
  }
  const context = createContext({
    Buffer,
    TextDecoder,
    performance: { now: () => clock.now },
    process: { env: {}, getuid: () => 1000, getgid: () => 1000 },
    Bun: {
      spawnSync(command, options) {
        const args = Array.from(command);
        if (args[0] === "docker") {
          return { exitCode: 0, stdout: Buffer.alloc(0), stderr: Buffer.alloc(0) };
        }
        if (args[0] !== "ssh-keyscan") throw new Error(`未预期的进程: ${args[0]}`);
        if (!Number.isFinite(options.timeout) || options.timeout <= 0) {
          throw new Error("ssh-keyscan 没有有效的子进程期限");
        }
        childCalls.push({ args, timeout: options.timeout });
        const response = plan.shift() ?? { elapsedMs: options.timeout + 1, exitCode: 1 };
        const elapsedMs = response.elapsedMs ?? 0;
        if (elapsedMs > options.timeout) {
          clock.now += options.timeout;
          return {
            exitCode: null,
            signalCode: "SIGTERM",
            exitedDueToTimeout: true,
            stdout: Buffer.alloc(0),
            stderr: Buffer.from(response.stderr ?? ""),
          };
        }
        clock.now += elapsedMs;
        return {
          exitCode: response.exitCode,
          signalCode: response.signalCode ?? null,
          exitedDueToTimeout: false,
          stdout: Buffer.from(response.stdout ?? ""),
          stderr: Buffer.from(response.stderr ?? ""),
        };
      },
      sleepSync(milliseconds) {
        sleeps.push(milliseconds);
        clock.now += milliseconds;
      },
    },
  });
  const module = new SourceTextModule(fs.readFileSync(new URL(entryUrl), "utf8"), {
    identifier: entryUrl,
    context,
  });
  const dependencies = new Map([
    ["node:fs", {
      appendFileSync: fs.appendFileSync,
      chmodSync: fs.chmodSync,
      cpSync: fs.cpSync,
      existsSync: fs.existsSync,
      mkdirSync: fs.mkdirSync,
      mkdtempSync: fs.mkdtempSync,
      readFileSync: fs.readFileSync,
      renameSync: fs.renameSync,
      rmSync: fs.rmSync,
      statSync: fs.statSync,
      writeFileSync: fs.writeFileSync,
    }],
    ["node:path", { default: path.default }],
    ["./process.mjs", {
      runChecked(command) {
        const args = Array.from(command);
        checkedCommands.push(args);
        if (args[0] === "ssh-keygen") {
          if (failKnownHostUpdate) throw new Error("模拟 ssh-keygen 更新失败");
          const targetHost = args[args.indexOf("-R") + 1];
          const knownHostsIndex = args.indexOf("-f") + 1;
          const stagedKnownHostsPath = args[knownHostsIndex];
          const contents = fs.readFileSync(stagedKnownHostsPath, "utf8");
          const lineEnding = contents.includes("\r\n") ? "\r\n" : "\n";
          const hasTrailingLineEnding = contents.endsWith(lineEnding);
          const lines = contents.split(/\r?\n/);
          if (hasTrailingLineEnding) lines.pop();
          const remainingLines = lines.filter((line) => {
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith("#")) return true;
            const hostAliases = trimmed.split(/\s+/, 1)[0].split(",");
            return !hostAliases.includes(targetHost) && !removedKnownHostLines.includes(line);
          });
          fs.writeFileSync(
            stagedKnownHostsPath,
            remainingLines.join(lineEnding) + (hasTrailingLineEnding && remainingLines.length ? lineEnding : ""),
            "utf8",
          );
          return { exitCode: 0, stdout: "", stderr: "" };
        }
        if (args[0] !== "docker") throw new Error(`未预期的检查命令: ${args[0]}`);
        return { exitCode: 0, stdout: "", stderr: "" };
      },
    }],
  ]);
  await module.link((specifier) => {
    const exports = dependencies.get(specifier);
    if (!exports) throw new Error(`未预期的 Docker provision 依赖: ${specifier}`);
    return new SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
  });
  await module.evaluate();
  const target = {
    id: "docker-host-key-fixture",
    platform: "linux",
    provisioner: "docker",
    docker: {
      composeProject: "docker-host-key-fixture",
      image: "fixture:latest",
      persistentRoot: path.join(fixtureRoot, "persistent"),
    },
    ssh: {
      host: "fixture.invalid",
      port: 22222,
      knownHostsFile: knownHostsPath,
    },
  };
  let result = null;
  let error = null;
  try {
    result = module.namespace.provisionDockerTarget({ target, projectRoot: fixtureRoot });
  } catch (caught) {
    error = { name: caught?.name ?? typeof caught, message: String(caught?.message ?? caught) };
  }
  return {
    result,
    error,
    childCalls,
    sleeps,
    checkedCommands,
    knownHosts: fs.existsSync(knownHostsPath) ? fs.readFileSync(knownHostsPath, "utf8") : null,
  };
}

function invokeDockerProvision(
  plan,
  existingKnownHosts = null,
  failKnownHostUpdate = false,
  removedKnownHostLines = [],
) {
  const root = createFixtureRoot();
  const entryUrl = new URL("../../tools/cross-platform-development-targets/docker-target.mjs", import.meta.url).href;
  const run = spawnSync("node", [
    "--experimental-vm-modules",
    "--input-type=module",
    "--eval",
    `${observeDockerProvision.toString()}\nconsole.log(JSON.stringify(await observeDockerProvision(...JSON.parse(process.argv[1]))));`,
    JSON.stringify([
      entryUrl,
      root,
      plan,
      existingKnownHosts,
      failKnownHostUpdate,
      removedKnownHostLines,
    ]),
  ], { cwd: root, encoding: "utf8", timeout: 5000 });
  if (run.error || run.status !== 0) {
    throw new Error(run.stderr || `Docker provision 探针退出: ${run.status}`, { cause: run.error });
  }
  return JSON.parse(run.stdout);
}

function expectShellTransport(observation, remoteCommand) {
  const { command, destination, identityFile, knownHostsFile } = observation;
  expect(command[0]).toBe("ssh");
  expect(command[1]).toBe("-t");
  expect(command).toContain(identityFile);
  expect(command).toContain("22222");
  expect(command).toContain("StrictHostKeyChecking=yes");
  expect(command).toContain(`UserKnownHostsFile=${knownHostsFile}`);
  expect(command).not.toContain("fixture identity only");
  expect(command.at(-1)).toBe(remoteCommand ?? destination);
  if (remoteCommand === undefined) expect(command).not.toContain(undefined);
}

describe("runChecked", () => {
  test("spawnSync 创建失败时附调用方 label 并保留原始 cause", () => {
    const label = "Docker 目标 fixture 创建阶段";
    const cause = new Error("native spawn failure");
    const spawnSync = spyOn(Bun, "spawnSync").mockImplementation(() => {
      throw cause;
    });
    try {
      let thrown;
      try {
        runChecked(["PRIVATE_COMMAND", "PRIVATE_ARGUMENT"], {
          environment: { PRIVATE_ENV: "PRIVATE_VALUE" },
          label,
        });
      } catch (error) {
        thrown = error;
      }

      expect(thrown).toBeInstanceOf(Error);
      expect(thrown.message).toBe(`${label}启动失败: native spawn failure`);
      expect(thrown.cause).toBe(cause);
      expect(thrown.message).not.toContain("PRIVATE_COMMAND");
      expect(thrown.message).not.toContain("PRIVATE_ARGUMENT");
      expect(thrown.message).not.toContain("PRIVATE_ENV");
      expect(thrown.message).not.toContain("PRIVATE_VALUE");
    } finally {
      spawnSync.mockRestore();
    }
  });

  test.skipIf(process.platform === "win32")("子进程被真实信号终止时报告 signal", () => {
    const label = "SSH fixture 执行阶段";
    let thrown;
    try {
      runChecked(["sh", "-c", "kill -TERM $$"], { label });
    } catch (error) {
      thrown = error;
    }

    expect(thrown).toBeInstanceOf(Error);
    expect(thrown.message).toContain(`${label}失败(exit=null, signal=SIGTERM)`);
    expect(thrown.message).not.toContain("kill -TERM");
  });

  test("成功返回保持裁剪后的 stdout、stderr 和 exitCode", () => {
    const spawnSync = spyOn(Bun, "spawnSync").mockReturnValue({
      exitCode: 0,
      signalCode: null,
      stdout: Buffer.from(" ready \n"),
      stderr: Buffer.from(" notice \n"),
    });
    try {
      expect(runChecked(["fixture-command"], { label: "fixture" })).toEqual({
        stdout: "ready",
        stderr: "notice",
        exitCode: 0,
      });
    } finally {
      spawnSync.mockRestore();
    }
  });

  test("非零退出的 stdout 和 stderr 错误格式不变", () => {
    const label = "目标 fixture 动作阶段";
    const spawnSync = spyOn(Bun, "spawnSync").mockReturnValue({
      exitCode: 23,
      signalCode: null,
      stdout: Buffer.from(" partial output \n"),
      stderr: Buffer.from(" command failed \n"),
    });
    try {
      let thrown;
      try {
        runChecked(["fixture-command"], { label });
      } catch (error) {
        thrown = error;
      }

      expect(thrown).toBeInstanceOf(Error);
      expect(thrown.message).toBe(
        `${label}失败(exit=23)\nstdout:\npartial output\nstderr:\ncommand failed`,
      );
    } finally {
      spawnSync.mockRestore();
    }
  });
});

describe("runCheckedWithInput", () => {
  test("真实子进程接收 UTF-8 中文输入并回显", async () => {
    const input = "目标动作：同步中文管理脚本\n";
    const result = await runCheckedWithInput(
      [process.execPath, "-e", "process.stdin.pipe(process.stdout)"],
      input,
      { label: "Linux 目标 fixture 动作阶段" },
    );

    expect(result).toEqual({ exitCode: 0, stdout: input.trim(), stderr: "" });
  });

  test("spawn 创建失败时附调用方 label 并保留原始 cause", async () => {
    const label = "Windows 目标 fixture 动作阶段";
    const cause = new Error("native spawn failure");
    const spawn = spyOn(Bun, "spawn").mockImplementation(() => {
      throw cause;
    });
    try {
      let thrown;
      try {
        await runCheckedWithInput(["PRIVATE_COMMAND"], "PRIVATE_INPUT", { label });
      } catch (error) {
        thrown = error;
      }

      expect(thrown).toBeInstanceOf(Error);
      expect(thrown.message).toBe(`${label}启动失败: native spawn failure`);
      expect(thrown.cause).toBe(cause);
      expect(thrown.message).not.toContain("PRIVATE_COMMAND");
      expect(thrown.message).not.toContain("PRIVATE_INPUT");
    } finally {
      spawn.mockRestore();
    }
  });

  test("非零退出保留 stdout、stderr 诊断格式", async () => {
    const label = "目标 fixture 动作阶段";
    let thrown;
    try {
      await runCheckedWithInput(
        [
          process.execPath,
          "-e",
          "process.stdin.resume(); process.stdin.on('end', () => { process.stdout.write('partial output\\n'); process.stderr.write('command failed\\n'); process.exitCode = 23; });",
        ],
        "fixture input",
        { label },
      );
    } catch (error) {
      thrown = error;
    }

    expect(thrown).toBeInstanceOf(Error);
    expect(thrown.message).toBe(
      `${label}失败(exit=23)\nstdout:\npartial output\nstderr:\ncommand failed`,
    );
  });

  test.skipIf(process.platform === "win32")("真实信号终止时报告 signal", async () => {
    const label = "Linux SSH fixture 动作阶段";
    let thrown;
    try {
      await runCheckedWithInput(
        [
          process.execPath,
          "-e",
          "process.stdin.resume(); process.stdin.on('end', () => process.kill(process.pid, 'SIGTERM'));",
        ],
        "fixture input",
        { label },
      );
    } catch (error) {
      thrown = error;
    }

    expect(thrown).toBeInstanceOf(Error);
    expect(thrown.message).toContain(`${label}失败(exit=`);
    expect(thrown.message).toContain(", signal=SIGTERM)");
  });


});

describe("Docker provision SSH host key deadline", () => {
  test("每次 ssh-keyscan 都收到剩余期限，单调截止后不再启动子进程", () => {
    const observations = Array.from({ length: 60 }, (_, index) => ({
      elapsedMs: 1,
      exitCode: 1,
      stderr: `ssh-keyscan 拒绝连接 ${index}`,
    }));
    const observation = invokeDockerProvision(observations, "existing-host-key\n");

    expect(observation.result).toBeNull();
    expect(observation.error).toMatchObject({ name: "Error" });
    expect(observation.error.message).toContain("Docker 目标 docker-host-key-fixture (linux)");
    expect(observation.error.message).toContain("host=fixture.invalid, port=22222");
    expect(observation.error.message).toContain("ssh-keyscan 拒绝连接 59");
    expect(observation.childCalls).toHaveLength(60);
    expect(observation.childCalls[0].timeout).toBe(30_000);
    expect(observation.childCalls.at(-1).timeout).toBe(441);
    expect(observation.childCalls.every(({ timeout }) => timeout > 0 && timeout <= 30_000)).toBe(true);
    expect(observation.sleeps).toHaveLength(60);
    expect(observation.sleeps.reduce((total, value) => total + value, 0)).toBe(29_940);
    expect(observation.knownHosts).toBe("existing-host-key\n");
    expect(observation.checkedCommands).toHaveLength(1);
  });

  test("子进程超过总期限时由spawnSync中断且保留known_hosts", () => {
    const observation = invokeDockerProvision([
      { elapsedMs: 30_001, stderr: "ssh-keyscan timed out" },
    ], "existing-host-key\n");

    expect(observation.error.message).toContain("超过本次剩余子进程期限 30000 ms");
    expect(observation.error.message).toContain("ssh-keyscan timed out");
    expect(observation.childCalls).toEqual([
      { args: ["ssh-keyscan", "-p", "22222", "-t", "ed25519", "fixture.invalid"], timeout: 30_000 },
    ]);
    expect(observation.sleeps).toEqual([]);
    expect(observation.knownHosts).toBe("existing-host-key\n");
  });

  test("known_hosts 更新命令失败时保留原文件", () => {
    const existingKnownHosts = "[fixture.invalid]:22222 ssh-ed25519 AAA-existing-key\n";
    const observation = invokeDockerProvision([
      { elapsedMs: 1, exitCode: 0, stdout: "[fixture.invalid]:22222 ssh-ed25519 AAA-new-key\n" },
    ], existingKnownHosts, true);

    expect(observation.error.message).toContain("模拟 ssh-keygen 更新失败");
    expect(observation.knownHosts).toBe(existingKnownHosts);
    expect(observation.checkedCommands).toHaveLength(2);
  });

  test("保留未命中的 pattern-list 并替换目标 key", () => {
    const existingKnownHosts = [
      "[fixture.invalid]:22222 ssh-ed25519 AAA-existing-key",
      "*.unrelated.invalid,!blocked.unrelated.invalid ssh-ed25519 AAA-unrelated-pattern",
      "[*fixture.invalid]:22222,![fixture.invalid]:22222 ssh-ed25519 AAA-excluded-target-pattern",
      "",
    ].join("\n");
    const observation = invokeDockerProvision([
      { elapsedMs: 1, exitCode: 0, stdout: "[fixture.invalid]:22222 ssh-ed25519 AAA-new-key\n" },
    ], existingKnownHosts);

    expect(observation.error).toBeNull();
    expect(observation.knownHosts).toBe([
      "*.unrelated.invalid,!blocked.unrelated.invalid ssh-ed25519 AAA-unrelated-pattern",
      "[*fixture.invalid]:22222,![fixture.invalid]:22222 ssh-ed25519 AAA-excluded-target-pattern",
      "[fixture.invalid]:22222 ssh-ed25519 AAA-new-key",
      "",
    ].join("\n"));
  });

  test("命中 pattern-list 时拒绝整行删除并保留原文件", () => {
    const matchedPatternRow = "[*fixture.invalid]:22222 ssh-ed25519 AAA-matched-pattern";
    const existingKnownHosts = `${matchedPatternRow}\n`;
    const observation = invokeDockerProvision([
      { elapsedMs: 1, exitCode: 0, stdout: "[fixture.invalid]:22222 ssh-ed25519 AAA-new-key\n" },
    ], existingKnownHosts, false, [matchedPatternRow]);

    expect(observation.error.message).toContain("拒绝改变其匹配语义");
    expect(observation.knownHosts).toBe(existingKnownHosts);
  });

  test("期限后才返回的host key不会写入known_hosts，期限内成功仍可安装", () => {
    const late = invokeDockerProvision([
      {
        elapsedMs: 30_000,
        exitCode: 0,
        stdout: "fixture.invalid ssh-ed25519 AAA-late-key\n",
      },
    ], "existing-host-key\n");
    expect(late.error.message).toContain("在观测期限后返回 host key");
    expect(late.knownHosts).toBe("existing-host-key\n");

    const success = invokeDockerProvision([
      { elapsedMs: 1, exitCode: 1, stderr: "暂不可用" },
      { elapsedMs: 1, exitCode: 0, stdout: "[fixture.invalid]:22222 ssh-ed25519 AAA-fixture-key\n" },
    ], [
      "# target,alias 仅属于注释",
      "[fixture.invalid]:22222,alias.fixture.invalid ssh-ed25519 AAA-old-target-key target-comment",
      "unrelated.fixture.invalid ssh-rsa AAA-unrelated-key",
      "",
    ].join("\n"));
    expect(success.error).toBeNull();
    expect(success.knownHosts).toBe([
      "# target,alias 仅属于注释",
      "alias.fixture.invalid ssh-ed25519 AAA-old-target-key target-comment",
      "unrelated.fixture.invalid ssh-rsa AAA-unrelated-key",
      "[fixture.invalid]:22222 ssh-ed25519 AAA-fixture-key",
      "",
    ].join("\n"));
    expect(success.checkedCommands[1].slice(0, 4)).toEqual([
      "ssh-keygen",
      "-R",
      "[fixture.invalid]:22222",
      "-f",
    ]);
    expect(success.childCalls).toHaveLength(2);
    expect(success.sleeps).toEqual([500]);
  });
});

describe("跨平台开发目标配置", () => {
  test("加载示例并严格校验目标", () => {
    const configuration = loadTargetConfiguration({
      configPath: path.join(
        projectRoot,
        "tools/cross-platform-development-targets/targets.example.jsonc",
      ),
      projectRoot,
    });
    const target = selectTarget(configuration, "docker-debian");
    expect(target.platform).toBe("linux");
    expect(path.isAbsolute(target.ssh.identityFile)).toBe(true);
    expect(path.isAbsolute(target.docker.persistentRoot)).toBe(true);
  });

  test("schema 错误不回显配置中的秘密值", () => {
    const root = createFixtureRoot();
    const configPath = path.join(root, "targets.jsonc");
    writeFileSync(
      configPath,
      JSON.stringify({ version: 1, targets: [], secret: "DO_NOT_PRINT_THIS" }),
      "utf8",
    );
    expect(() => loadTargetConfiguration({ configPath, projectRoot })).toThrow(
      /additional properties/,
    );
    try {
      loadTargetConfiguration({ configPath, projectRoot });
    } catch (error) {
      expect(String(error)).not.toContain("DO_NOT_PRINT_THIS");
    }
  });

  test("命令参数要求显式 target 并验证 profile", () => {
    expect(
      parseTargetCliArgs(["sync", "docker-debian", "--activate"]),
    ).toMatchObject({
      command: "sync",
      targetId: "docker-debian",
      options: { activate: true, profile: "development" },
    });
    expect(parseTargetCliArgs(["shell", "docker-debian", "--help"])).toEqual({
      help: true,
    });
    expect(
      parseTargetCliArgs(["shell", "docker-debian", "--", "python", "--help"]),
    ).toMatchObject({
      help: false,
      command: "shell",
      targetId: "docker-debian",
      options: { passthrough: ["python", "--help"] },
    });
    expect(
      parseTargetCliArgs(["test", "docker-debian", "--", "bun", "test", "-h"]),
    ).toMatchObject({
      help: false,
      command: "test",
      targetId: "docker-debian",
      options: { passthrough: ["bun", "test", "-h"] },
    });
    expect(() =>
      parseTargetCliArgs(["start", "docker-debian", "--profile", "unknown"]),
    ).toThrow(/profile/);
  });
});

describe("Git 脏工作区快照", () => {
  test("包含修改删除和未跟踪文件且不改变真实 index", () => {
    const root = createFixtureRoot();
    initializeRepository(root);
    writeFileSync(path.join(root, "tracked.txt"), "after\n", "utf8");
    rmSync(path.join(root, "deleted.txt"));
    writeFileSync(path.join(root, "new.txt"), "new\n", "utf8");
    writeFileSync(path.join(root, ".env"), "SECRET=not-in-git\n", "utf8");
    const statusBefore = command(root, ["git", "status", "--porcelain"]);
    const cachedBefore = command(root, ["git", "diff", "--cached"]);

    const snapshot = createWorkspaceSnapshot({ projectRoot: root });

    expect(command(root, ["git", "status", "--porcelain"])).toBe(statusBefore);
    expect(command(root, ["git", "diff", "--cached"])).toBe(cachedBefore);
    expect(
      command(root, ["git", "show", `${snapshot.commit}:tracked.txt`]),
    ).toBe("after");
    expect(command(root, ["git", "show", `${snapshot.commit}:new.txt`])).toBe(
      "new",
    );
    expect(
      Bun.spawnSync(
        ["git", "cat-file", "-e", `${snapshot.commit}:deleted.txt`],
        {
          cwd: root,
        },
      ).exitCode,
    ).not.toBe(0);
    expect(snapshot.files).not.toContain(".env");
  });

  test("拒绝包含未提交内容的已初始化子模块", () => {
    const child = createFixtureRoot();
    initializeRepository(child);
    const parent = createFixtureRoot();
    initializeRepository(parent);
    command(parent, [
      "git",
      "-c",
      "protocol.file.allow=always",
      "submodule",
      "add",
      "-q",
      child,
      "vendor/child",
    ]);
    command(parent, ["git", "commit", "-qam", "add submodule"]);
    writeFileSync(
      path.join(parent, "vendor/child/tracked.txt"),
      "dirty\n",
      "utf8",
    );
    expect(() => assertCleanInitializedSubmodules(parent)).toThrow(/子模块/);
  });
});

describe("平台 adapter 边界", () => {
  test("SSH 命令强制 host key 校验且不包含密钥内容", () => {
    const root = createFixtureRoot();
    const identityFile = path.join(root, "id_ed25519");
    writeFileSync(identityFile, "fixture-identity", "utf8");
    const target = {
      id: "linux",
      ssh: {
        host: "127.0.0.1",
        port: 22222,
        user: "boxteam",
        identityFile,
        knownHostsFile: path.join(root, "known_hosts"),
      },
    };
    const result = sshCommand(target, "echo ready");
    expect(result).toContain("StrictHostKeyChecking=yes");
    expect(result.join(" ")).not.toContain("PRIVATE KEY");
  });

  test.skipIf(process.platform === "win32")("SSH transport 在首次使用前收紧 identity 文件权限", () => {
    const root = createFixtureRoot();
    const identityFile = path.join(root, "id_ed25519");
    writeFileSync(identityFile, "private-key", "utf8");
    chmodSync(identityFile, 0o660);
    const target = {
      id: "linux-permissions",
      ssh: {
        host: "127.0.0.1",
        port: 22222,
        user: "boxteam",
        identityFile,
        knownHostsFile: path.join(root, "known_hosts"),
      },
    };

    const result = sshCommand(target, "echo ready");

    expect(result).toContain(identityFile);
    expect(statSync(identityFile).mode & 0o777).toBe(0o600);
  });

  test(".env 哈希不一致时快速失败", () => {
    expect(() => assertMatchingSha256("local", "remote", "linux")).toThrow(
      /SHA-256 校验失败/,
    );
  });

  test.each(["linux", "windows"])("%s .env 上传成功后校验并安装临时文件", (platform) => {
    const observation = invokeEnvironmentUpload(platform);
    expect(observation.error).toBeNull();
    expect(observation.result).toMatchObject({ bytes: 19 });
    expect(observation.result.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(observation.calls.map((call) => call.action ?? call.kind)).toEqual([
      "scp",
      "hash-file",
      "install-env",
    ]);
    const [hashAction, installAction] = observation.calls.filter((call) => call.kind === "action");
    expect(hashAction.args[0]).toMatch(/\.env\.uploading-/);
    expect(installAction.args).toEqual([hashAction.args[0], `${path.dirname(hashAction.args[0])}/.env`]);
  });

  test.each(["linux", "windows"])("%s 上传动作失败后清理上传临时文件并保留原始错误", (platform) => {
    for (const failures of [
      { scp: "injected scp failure" },
      { "hash-file": "injected hash-file failure" },
    ]) {
      const observation = invokeEnvironmentUpload(platform, failures);
      expect(observation.error).toMatchObject({ name: "Error", message: Object.values(failures)[0] });
      expect(observation.calls.filter((call) => call.kind === "action").map((call) => call.action)).toEqual(
        failures.scp ? ["remove-upload"] : ["hash-file", "remove-upload"],
      );
      const uploadAction = observation.calls.find((call) => call.action === "remove-upload");
      expect(uploadAction.args[0]).toMatch(/\.env\.uploading-/);
    }
  });

  test.each(["linux", "windows"])("%s 摘要冲突且清理失败时保留两份错误", (platform) => {
    const observation = invokeEnvironmentUpload(platform, {
      remoteHash: "remote-hash-mismatch",
      "remove-upload": "injected remove-upload failure",
    });
    expect(observation.error.message).toContain("SHA-256 校验失败");
    expect(observation.error.message).toContain("injected remove-upload failure");
    expect(observation.error).toMatchObject({
      name: "AggregateError",
      message: expect.stringContaining("SHA-256 校验失败"),
      cause: { name: "Error", message: expect.stringContaining("SHA-256 校验失败") },
      errors: [
        { name: "Error", message: expect.stringContaining("SHA-256 校验失败") },
        { name: "Error", message: "injected remove-upload failure" },
      ],
    });
    expect(observation.calls.filter((call) => call.kind === "action").map((call) => call.action)).toEqual([
      "hash-file",
      "remove-upload",
    ]);
  });

  test.each(["linux", "windows"])("%s install-env 失败后回收剩余上传临时文件", (platform) => {
    const observation = invokeEnvironmentUpload(platform, { "install-env": "injected install-env failure" });
    expect(observation.error).toMatchObject({ name: "Error", message: "injected install-env failure" });
    expect(observation.calls.filter((call) => call.kind === "action").map((call) => call.action)).toEqual([
      "hash-file",
      "install-env",
      "remove-upload",
    ]);
  });

  test("Windows adapter 保留真实 VMware 验证 TODO 且不使用 POSIX 生命周期命令", () => {
    const script = readFileSync(
      path.join(
        projectRoot,
        "tools/cross-platform-development-targets/windows/Manage-Target.ps1",
      ),
      "utf8",
    );
    expect(script).toContain("TODO");
    expect(script).not.toMatch(/\bnohup\b/);
    expect(script).not.toMatch(/\bkill\b/);
    expect(script).toContain("BOXTEAM_TARGET_ARGUMENTS_BASE64");
    expect(script).toContain("Assert-NativeCommand");
    expect(script).toContain(".venv\\Scripts\\python.exe");
    expect(script).toContain(".boxteams-dev");
    expect(script).toContain(".boxteams");
    expect(script).toContain("开发服务端口已被占用");
    expect(script).toContain("bun install --force --frozen-lockfile");
    expect(script).toContain("BOXTEAM_SERVICE_LOG_PATH");
    expect(script).toContain("BOXTEAM_SERVICE_LOG_CAPTURED");
  });

  test("Linux adapter 快速暴露冲突并按环境版本重建目标 .venv", () => {
    const script = readFileSync(
      path.join(
        projectRoot,
        "tools/cross-platform-development-targets/linux/manage-target.sh",
      ),
      "utf8",
    );
    expect(script).toContain("set -eu");
    expect(script).toContain("开发服务端口已被占用");
    expect(script).toContain('"$venv_version" != "$system_version"');
    expect(script).toContain('rm -rf -- "$repository/.venv"');
    expect(script).toContain("bun install --force --frozen-lockfile");
    expect(script).toContain('BOXTEAM_SERVICE_LOG_PATH="$log_file"');
    expect(script).toContain("BOXTEAM_SERVICE_LOG_CAPTURED=1");
    expect(script).toContain("$target_home/.boxteams-dev");
    expect(script).toContain("$target_home/.boxteams");
    expect(script).not.toContain('kill "$(cat');
  });
});

describe("交互 shell 的 SSH 命令组装", () => {
  test("Windows 默认打开配置的交互 shell，不发送 POSIX 命令", () => {
    expectShellTransport(invokeShellCommand("windows"), undefined);
  });

  test("Linux 默认仍启动登录 shell", () => {
    expectShellTransport(invokeShellCommand("linux"), 'cd && exec "$SHELL" -l');
  });

  test("shell 透传管理 help 参数仍执行远端命令", () => {
    expectShellTransport(
      invokeShellCommand("linux", ["python", "--help"]),
      "python --help",
    );
  });

  for (const platform of ["windows", "linux"]) {
    test(`${platform}保留显式透传命令`, () => {
      expectShellTransport(invokeShellCommand(platform, ["echo", "fixture-command"]), "echo fixture-command");
    });
  }
});

import { readFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { parse, printParseErrorCode } from "jsonc-parser";

const projectRoot = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const matrixPath = path.join(projectRoot, "tests", "runner", "matrix.jsonc");

// 套件默认上限：无界增长的用例必须先撞上限失败，而不是把整机内存吃到 OOM。
// 实测最慢套件 unit-python（4825 用例）约 24:20、峰值 683MB，故默认超时留约 3.7 倍余量。
const defaultTimeoutMs = 90 * 60 * 1000;
const timeoutGraceMs = 30 * 1000;
const defaultDataLimitKb = 8 * 1024 * 1024;
const isPosix = process.platform !== "win32";

function readArgument(name) {
  const prefix = `--${name}=`;
  const value = process.argv.slice(2).find((argument) => argument.startsWith(prefix));
  return value?.slice(prefix.length) ?? null;
}

function validateSuite(suite) {
  if (!suite || typeof suite !== "object") throw new Error("测试 suite 必须是对象");
  if (typeof suite.id !== "string" || !suite.id) throw new Error("测试 suite 缺少 id");
  if (typeof suite.enabled !== "boolean") {
    throw new Error(`测试 suite ${suite.id} 缺少 enabled`);
  }
  if (suite.enabled && (!Array.isArray(suite.command) || suite.command.length === 0)) {
    throw new Error(`已启用测试 suite ${suite.id} 缺少 command`);
  }
  if (suite.timeoutMs !== undefined && (!Number.isInteger(suite.timeoutMs) || suite.timeoutMs <= 0)) {
    throw new Error(`测试 suite ${suite.id} 的 timeoutMs 必须是正整数`);
  }
  if (suite.dataLimitKb !== undefined && (!Number.isInteger(suite.dataLimitKb) || suite.dataLimitKb <= 0)) {
    throw new Error(`测试 suite ${suite.id} 的 dataLimitKb 必须是正整数`);
  }
}

// 用 ulimit -d 限定数据段：harness 一旦无界增长就立刻 MemoryExhaustion 退出，
// 不再把整机内存吃满；exec 保证退出码与信号语义不变。
function buildSuiteCommand(suite, dataLimitKb) {
  if (!isPosix) return suite.command;
  return [
    "bash",
    "-c",
    'ulimit -d "$1" || exit 97; shift; exec "$@"',
    "bash",
    String(dataLimitKb),
    ...suite.command,
  ];
}

// 只杀自己创建的进程组：bun/pytest 派生的孙进程不会被遗留成游离进程。
function killProcessGroup(pid, signal) {
  try {
    process.kill(isPosix ? -pid : pid, signal);
  } catch (error) {
    if (error?.code === "ESRCH") return;
    throw error;
  }
}

async function loadMatrix() {
  const errors = [];
  const source = await readFile(matrixPath, "utf8");
  const matrix = parse(source, errors, { allowTrailingComma: true });
  if (errors.length > 0) {
    const details = errors
      .map((error) => `${printParseErrorCode(error.error)}@${error.offset}`)
      .join(", ");
    throw new Error(`测试矩阵 JSONC 无效: ${details}`);
  }
  if (matrix?.schemaVersion !== 1 || !Array.isArray(matrix.suites)) {
    throw new Error("测试矩阵必须包含 schemaVersion=1 和 suites 数组");
  }
  matrix.suites.forEach(validateSuite);
  return matrix;
}

function unmetPrerequisites(suite) {
  return (suite.requiredEnvironment ?? []).filter((name) => {
    const value = process.env[name];
    return !value || value === "0";
  });
}

async function runSuite(suite) {
  if (!suite.enabled) {
    process.stdout.write(`${JSON.stringify({ suite: suite.id, status: "skipped", reason: suite.todo })}\n`);
    return 0;
  }
  const missing = unmetPrerequisites(suite);
  if (missing.length > 0) {
    process.stdout.write(
      `${JSON.stringify({ suite: suite.id, status: "UNMET_PREREQUISITE", missing })}\n`,
    );
    return 2;
  }
  const timeoutMs = suite.timeoutMs ?? defaultTimeoutMs;
  const child = Bun.spawn(buildSuiteCommand(suite, suite.dataLimitKb ?? defaultDataLimitKb), {
    cwd: projectRoot,
    env: {
      ...process.env,
      BOXTEAM_TEST_SUITE: suite.id,
      BOXTEAM_TEST_RUN_ID: process.env.BOXTEAM_TEST_RUN_ID ?? `${Date.now()}-${process.pid}`,
    },
    stdin: "inherit",
    stdout: "inherit",
    stderr: "inherit",
    detached: true,
  });
  let timedOut = false;
  // detached 的子进程组收不到终端的 Ctrl-C，必须自己转发；否则中断 runner 会留下游离的测试进程。
  const onInterrupt = () => {
    killProcessGroup(child.pid, "SIGTERM");
    process.exit(130);
  };
  process.once("SIGINT", onInterrupt);
  process.once("SIGTERM", onInterrupt);
  const termTimer = setTimeout(() => {
    timedOut = true;
    killProcessGroup(child.pid, "SIGTERM");
  }, timeoutMs);
  const killTimer = setTimeout(() => killProcessGroup(child.pid, "SIGKILL"), timeoutMs + timeoutGraceMs);
  const exitCode = await child.exited;
  clearTimeout(termTimer);
  clearTimeout(killTimer);
  process.off("SIGINT", onInterrupt);
  process.off("SIGTERM", onInterrupt);
  if (timedOut) {
    process.stdout.write(
      `${JSON.stringify({ suite: suite.id, status: "TIMEOUT", timeoutMs, signal: child.signalCode })}\n`,
    );
    return 3;
  }
  return exitCode;
}

const matrix = await loadMatrix();
if (process.argv.includes("--list")) {
  for (const suite of matrix.suites) {
    process.stdout.write(
      `${suite.id}\t${suite.layer}\t${suite.client ?? "system"}\t${suite.enabled ? "enabled" : "TODO"}\n`,
    );
  }
  process.exit(0);
}

const suiteId = readArgument("suite");
if (!suiteId) throw new Error("必须使用 --suite=<id> 选择测试套件；使用 --list 查看列表");
const suite = matrix.suites.find((item) => item.id === suiteId);
if (!suite) throw new Error(`测试矩阵中不存在 suite: ${suiteId}`);
process.exit(await runSuite(suite));

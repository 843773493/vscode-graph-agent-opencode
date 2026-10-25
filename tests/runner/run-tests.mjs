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
const isWindows = process.platform === "win32";
const isPosix = !isWindows;
const windowsJobRunnerPath = path.join(
  projectRoot,
  "tests",
  "support",
  "processes",
  "windows_job.py",
);

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
  if (isWindows) {
    return [
      "uv",
      "run",
      "--no-sync",
      "--",
      "python",
      windowsJobRunnerPath,
      "--memory-limit-kb",
      String(dataLimitKb),
      "--",
      ...suite.command,
    ];
  }
  return [
    "bash",
    "-c",
    'ulimit -d "$1" || exit 97; shift; exec "$@"',
    "bash",
    String(dataLimitKb),
    ...suite.command,
  ];
}

function isProcessRunning(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") return false;
    throw error;
  }
}

function killWindowsProcessTree(pid, force) {
  if (!isProcessRunning(pid)) return;
  const command = ["taskkill", "/T", ...(force ? ["/F"] : []), "/PID", String(pid)];
  const result = Bun.spawnSync(command, {
    cwd: projectRoot,
    stdout: "pipe",
    stderr: "pipe",
  });
  if (result.exitCode === 0 && result.signalCode === null) return;
  if (!isProcessRunning(pid)) return;
  const stdout = new TextDecoder().decode(result.stdout).trim();
  const stderr = new TextDecoder().decode(result.stderr).trim();
  throw new Error(
    `无法清理 Windows 测试进程树: ${command.join(" ")} ` +
      `exit=${String(result.exitCode)} signal=${result.signalCode ?? "none"}` +
      `${stdout ? `\n${stdout}` : ""}${stderr ? `\n${stderr}` : ""}`,
  );
}

// Windows 由共享Job Object helper回收suite后代；taskkill负责终止监督器进程树。
function terminateSuiteProcesses(pid, signal) {
  if (isWindows) {
    killWindowsProcessTree(pid, signal === "SIGKILL");
    return;
  }
  try {
    process.kill(-pid, signal);
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

function parseArguments(args) {
  let listCount = 0;
  let suiteCount = 0;
  let suiteId = null;
  for (const argument of args) {
    if (argument === "--list") {
      listCount += 1;
    } else if (argument === "--all") {
      throw new Error("不支持 --all；必须使用 --suite=<id> 选择单个测试套件");
    } else if (argument === "--suite") {
      throw new Error("--suite 缺少值；必须使用 --suite=<id>");
    } else if (argument.startsWith("--suite=")) {
      suiteCount += 1;
      suiteId = argument.slice("--suite=".length);
    } else {
      throw new Error(`未知参数: ${argument}`);
    }
  }
  if (listCount > 1) throw new Error("--list 不能重复");
  if (suiteCount > 1) throw new Error("--suite 不能重复");
  if (listCount > 0 && suiteCount > 0) throw new Error("--list 与 --suite 不能同时使用");
  if (listCount === 1) return { mode: "list" };
  if (suiteCount === 0) throw new Error("必须使用 --suite=<id> 选择测试套件；使用 --list 查看列表");
  if (!suiteId.trim()) throw new Error("--suite 的值不能为空");
  if (suiteId === "all") throw new Error("不支持 --suite=all；必须选择单个测试套件");
  return { mode: "suite", suiteId };
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
  let interrupted = false;
  let interruptKillTimer = null;
  // 首次中断给退出宽限，再次中断立即强杀；不能先退出runner而撤掉回收责任。
  const onInterrupt = () => {
    if (interrupted) {
      terminateSuiteProcesses(child.pid, "SIGKILL");
      return;
    }
    interrupted = true;
    clearTimeout(termTimer);
    terminateSuiteProcesses(child.pid, "SIGTERM");
    interruptKillTimer = setTimeout(
      () => terminateSuiteProcesses(child.pid, "SIGKILL"),
      timeoutGraceMs,
    );
  };
  const termTimer = setTimeout(() => {
    timedOut = true;
    terminateSuiteProcesses(child.pid, "SIGTERM");
  }, timeoutMs);
  const killTimer = setTimeout(
    () => terminateSuiteProcesses(child.pid, "SIGKILL"),
    timeoutMs + timeoutGraceMs,
  );
  process.on("SIGINT", onInterrupt);
  process.on("SIGTERM", onInterrupt);
  let exitCode;
  try {
    exitCode = await child.exited;
  } finally {
    clearTimeout(termTimer);
    clearTimeout(killTimer);
    clearTimeout(interruptKillTimer);
    process.off("SIGINT", onInterrupt);
    process.off("SIGTERM", onInterrupt);
    // POSIX根进程退出不等于进程组为空；Windows监督器关闭时由Job Object回收后代。
    if (isPosix) terminateSuiteProcesses(child.pid, "SIGKILL");
  }
  if (interrupted) return 130;
  if (timedOut) {
    process.stdout.write(
      `${JSON.stringify({ suite: suite.id, status: "TIMEOUT", timeoutMs, signal: child.signalCode })}\n`,
    );
    return 3;
  }
  return exitCode;
}

const matrix = await loadMatrix();
const selection = parseArguments(process.argv.slice(2));
if (selection.mode === "list") {
  for (const suite of matrix.suites) {
    process.stdout.write(
      `${suite.id}\t${suite.layer}\t${suite.client ?? "system"}\t${suite.enabled ? "enabled" : "TODO"}\n`,
    );
  }
  process.exit(0);
}

const suite = matrix.suites.find((item) => item.id === selection.suiteId);
if (!suite) throw new Error(`测试矩阵中不存在 suite: ${selection.suiteId}`);
process.exit(await runSuite(suite));

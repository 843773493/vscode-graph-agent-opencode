import { readFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { parse, printParseErrorCode } from "jsonc-parser";

const projectRoot = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const matrixPath = path.join(projectRoot, "tests", "runner", "matrix.jsonc");

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
  const child = Bun.spawn(suite.command, {
    cwd: projectRoot,
    env: {
      ...process.env,
      BOXTEAM_TEST_SUITE: suite.id,
      BOXTEAM_TEST_RUN_ID: process.env.BOXTEAM_TEST_RUN_ID ?? `${Date.now()}-${process.pid}`,
    },
    stdin: "inherit",
    stdout: "inherit",
    stderr: "inherit",
  });
  return child.exited;
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

import { createHash, randomUUID } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const PROCESS_RUN_ID = `pid-${process.pid}-${randomUUID()}`;

export const TEST_RESULT_STATUSES = Object.freeze([
  "passed",
  "failed",
  "skipped",
  "UNMET_PREREQUISITE",
]);

function assertInside(parent, child, label) {
  const relative = path.relative(parent, child);
  if (
    relative === "" || relative === ".." ||
    relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)
  ) {
    throw new Error(`${label}必须位于 ${parent} 内，实际为 ${child}`);
  }
  return relative;
}

function withoutFinalExtension(filePath) {
  const extension = path.extname(filePath);
  return extension ? filePath.slice(0, -extension.length) : filePath;
}

function runDirectoryName() {
  const runIdentity = process.env.BOXTEAM_TEST_RUN_ID ||
    process.env.PYTEST_XDIST_TESTRUNUID || PROCESS_RUN_ID;
  const workerId = process.env.PYTEST_XDIST_WORKER;
  const scopedIdentity = workerId ? `${runIdentity}-${workerId}` : runIdentity;
  const runKey = createHash("sha1").update(scopedIdentity).digest("hex").slice(0, 12);
  return `run-${runKey}`;
}

function pytestNodeDirectoryName() {
  const currentTest = process.env.PYTEST_CURRENT_TEST;
  if (currentTest === undefined) return null;
  const separator = currentTest.lastIndexOf(" (");
  const phase = currentTest.slice(separator + 2, -1);
  if (
    separator < 0 || !currentTest.endsWith(")") ||
    !["setup", "call", "teardown"].includes(phase)
  ) {
    throw new Error(`PYTEST_CURRENT_TEST 格式无效: ${currentTest}`);
  }
  const nodeId = currentTest.slice(0, separator);
  const nodeKey = createHash("sha1").update(nodeId).digest("hex").slice(0, 12);
  return `node-${nodeKey}`;
}

export class TestRunContext {
  #cleanups = [];
  #cleanupStarted = false;
  #closePromise = null;

  constructor({ projectRoot, testFile }) {
    this.projectRoot = path.resolve(projectRoot);
    this.testFile = path.resolve(this.projectRoot, testFile);
    const testsRoot = path.join(this.projectRoot, "tests");
    const relativeTestFile = assertInside(testsRoot, this.testFile, "测试文件");
    this.testId = withoutFinalExtension(relativeTestFile).split(path.sep).join("/");
    this.outputRoot = path.join(
      this.projectRoot,
      "out",
      "tests",
      withoutFinalExtension(relativeTestFile),
    );
    this.runRoot = path.join(this.outputRoot, "runs", runDirectoryName());
    this.workspaceRoot = path.join(this.outputRoot, "workspace");
    const pytestNodeDirectory = pytestNodeDirectoryName();
    this.artifactsDir = pytestNodeDirectory
      ? path.join(this.runRoot, "nodes", pytestNodeDirectory, "artifacts")
      : path.join(this.runRoot, "artifacts");
    this.boxteamHome = path.join(this.outputRoot, "boxteam-home");
  }

  static fromTestFile(testFile, projectRoot = process.cwd()) {
    return new TestRunContext({ projectRoot, testFile });
  }

  async prepare() {
    await Promise.all([
      mkdir(this.workspaceRoot, { recursive: true }),
      mkdir(this.artifactsDir, { recursive: true }),
      mkdir(this.boxteamHome, { recursive: true }),
    ]);
    return this;
  }

  addCleanup(label, cleanup) {
    if (this.#cleanupStarted) throw new Error("测试运行上下文已经关闭，不能再注册清理动作");
    if (typeof cleanup !== "function") {
      throw new TypeError(`清理动作 ${label} 必须是函数`);
    }
    this.#cleanups.push({ label, cleanup });
  }

  async writeResult(status, details = {}) {
    if (!TEST_RESULT_STATUSES.includes(status)) {
      throw new Error(`未知测试结果状态: ${status}`);
    }
    await mkdir(this.artifactsDir, { recursive: true });
    const result = {
      schemaVersion: 1,
      testId: this.testId,
      status,
      recordedAt: new Date().toISOString(),
      details,
    };
    await writeFile(
      path.join(this.artifactsDir, "result.json"),
      `${JSON.stringify(result, null, 2)}\n`,
      "utf8",
    );
    return result;
  }

  async close() {
    if (this.#closePromise) return await this.#closePromise;
    if (this.#cleanupStarted) return;
    this.#cleanupStarted = true;
    const cleanups = this.#cleanups;
    this.#cleanups = [];
    // 先登记唯一在途清理，再执行回调；并发关闭不能把“停止登记”当成“资源已释放”。
    this.#closePromise = Promise.resolve().then(async () => {
      const failures = [];
      for (const item of cleanups.reverse()) {
        try {
          await item.cleanup();
        } catch (error) {
          failures.push(new Error(`清理 ${item.label} 失败`, { cause: error }));
        }
      }
      if (failures.length > 0) {
        throw new AggregateError(failures, "测试资源清理失败");
      }
    }).finally(() => {
      this.#closePromise = null;
    });
    await this.#closePromise;
  }
}

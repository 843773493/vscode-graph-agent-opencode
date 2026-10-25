import { describe, expect, test } from "bun:test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { TestRunContext } from "./run-context.mjs";

describe("TestRunContext", () => {
  test("按测试文件路径镜像正式输出目录", () => {
    const projectRoot = process.cwd();
    const context = TestRunContext.fromTestFile(
      path.join(projectRoot, "tests", "unit", "harness", "sample.test.mjs"),
      projectRoot,
    );
    expect(context.testId).toBe("unit/harness/sample.test");
    expect(context.outputRoot).toBe(
      path.join(projectRoot, "out", "tests", "unit", "harness", "sample.test"),
    );
    expect(context.workspaceRoot).toBe(path.join(context.outputRoot, "workspace"));
    expect(context.runRoot).toMatch(path.join(context.outputRoot, "runs", "run-"));
    expect(context.artifactsDir).toBe(path.join(context.runRoot, "artifacts"));
    expect(context.boxteamHome).toBe(path.join(context.outputRoot, "boxteam-home"));
  });

  test("不同测试运行使用独立产物目录", async () => {
    const projectRoot = process.cwd();
    const testFile = path.join(projectRoot, "tests", "harness", "js", "run-context.test.mjs");
    const previousRunId = process.env.BOXTEAM_TEST_RUN_ID;
    const previousWorkerId = process.env.PYTEST_XDIST_WORKER;
    try {
      process.env.BOXTEAM_TEST_RUN_ID = "first-run";
      process.env.PYTEST_XDIST_WORKER = "gw0";
      const first = TestRunContext.fromTestFile(testFile, projectRoot);
      const sameRun = TestRunContext.fromTestFile(testFile, projectRoot);
      process.env.PYTEST_XDIST_WORKER = "gw1";
      const parallelWorker = TestRunContext.fromTestFile(testFile, projectRoot);
      process.env.PYTEST_XDIST_WORKER = "gw0";
      process.env.BOXTEAM_TEST_RUN_ID = "second-run";
      const second = TestRunContext.fromTestFile(testFile, projectRoot);

      expect(first.runRoot).toBe(sameRun.runRoot);
      expect(first.artifactsDir).toBe(sameRun.artifactsDir);
      expect(first.runRoot).not.toBe(parallelWorker.runRoot);
      expect(first.runRoot).not.toBe(second.runRoot);
      expect(first.artifactsDir).not.toBe(second.artifactsDir);
      expect(first.boxteamHome).toBe(second.boxteamHome);
      expect(first.workspaceRoot).toBe(second.workspaceRoot);
      await first.writeResult("passed", { run: "first" });
      await second.writeResult("passed", { run: "second" });
      const firstResult = JSON.parse(await readFile(
        path.join(first.artifactsDir, "result.json"), "utf8",
      ));
      const secondResult = JSON.parse(await readFile(
        path.join(second.artifactsDir, "result.json"), "utf8",
      ));
      expect(firstResult.details).toEqual({ run: "first" });
      expect(secondResult.details).toEqual({ run: "second" });
    } finally {
      if (previousRunId === undefined) delete process.env.BOXTEAM_TEST_RUN_ID;
      else process.env.BOXTEAM_TEST_RUN_ID = previousRunId;
      if (previousWorkerId === undefined) delete process.env.PYTEST_XDIST_WORKER;
      else process.env.PYTEST_XDIST_WORKER = previousWorkerId;
    }
  });

  test("继承 pytest 用例身份时按用例隔离产物", async () => {
    const projectRoot = process.cwd();
    const testFile = path.join(projectRoot, "tests", "harness", "js", "run-context.test.mjs");
    const previousRunId = process.env.BOXTEAM_TEST_RUN_ID;
    const previousWorkerId = process.env.PYTEST_XDIST_WORKER;
    const previousCurrentTest = process.env.PYTEST_CURRENT_TEST;
    try {
      process.env.BOXTEAM_TEST_RUN_ID = "shared-run";
      process.env.PYTEST_XDIST_WORKER = "gw0";
      process.env.PYTEST_CURRENT_TEST = "tests/sample.py::first (call)";
      const first = TestRunContext.fromTestFile(testFile, projectRoot);
      await first.writeResult("passed", { case: "first" });

      process.env.PYTEST_CURRENT_TEST = "tests/sample.py::second (call)";
      const second = TestRunContext.fromTestFile(testFile, projectRoot);
      await second.writeResult("failed", { case: "second" });

      expect(first.runRoot).toBe(second.runRoot);
      expect(first.artifactsDir).not.toBe(second.artifactsDir);
      const firstResult = JSON.parse(await readFile(
        path.join(first.artifactsDir, "result.json"), "utf8",
      ));
      const secondResult = JSON.parse(await readFile(
        path.join(second.artifactsDir, "result.json"), "utf8",
      ));
      expect(firstResult.details).toEqual({ case: "first" });
      expect(secondResult.details).toEqual({ case: "second" });
    } finally {
      if (previousRunId === undefined) delete process.env.BOXTEAM_TEST_RUN_ID;
      else process.env.BOXTEAM_TEST_RUN_ID = previousRunId;
      if (previousWorkerId === undefined) delete process.env.PYTEST_XDIST_WORKER;
      else process.env.PYTEST_XDIST_WORKER = previousWorkerId;
      if (previousCurrentTest === undefined) delete process.env.PYTEST_CURRENT_TEST;
      else process.env.PYTEST_CURRENT_TEST = previousCurrentTest;
    }
  });

  test.each([
    ["..snapshots/中文 sample.test.mjs", "..snapshots/中文 sample.test"],
    ["..visible.test.mjs", "..visible.test"],
  ])("接受点号开头的合法测试路径 %s", (relativeTestFile, testId) => {
    const projectRoot = process.cwd();
    const context = TestRunContext.fromTestFile(
      path.join(projectRoot, "tests", relativeTestFile),
      projectRoot,
    );
    expect(context.testId).toBe(testId);
    expect(context.outputRoot).toBe(path.join(projectRoot, "out", "tests", testId));
  });

  test.each([
    "scripts/outside.mjs",
    "tests",
    "tests/..",
    "tests/../tests-backup/outside.mjs",
  ])("拒绝 tests 根目录及其外部路径 %s", relativeTestFile => {
    expect(() => TestRunContext.fromTestFile(
      path.join(process.cwd(), relativeTestFile),
      process.cwd(),
    )).toThrow("测试文件必须位于");
  });

  test("按注册逆序清理资源", async () => {
    const context = TestRunContext.fromTestFile(
      path.join(process.cwd(), "tests", "unit", "harness", "cleanup.test.mjs"),
    );
    const order = [];
    context.addCleanup("first", () => order.push("first"));
    context.addCleanup("second", () => order.push("second"));
    await context.close();
    expect(order).toEqual(["second", "first"]);
  });

  test("异步清理失败仍处理其余资源，保留标签和原始 cause", async () => {
    const context = TestRunContext.fromTestFile(
      path.join(process.cwd(), "tests", "unit", "harness", "cleanup-errors.test.mjs"),
    );
    const firstError = new TypeError("关闭第一个资源失败");
    const secondError = new Error("关闭第二个资源失败");
    const order = [];
    context.addCleanup("first", async () => { order.push("first"); throw firstError; });
    context.addCleanup("second", async () => { order.push("second"); throw secondError; });
    let failure;
    try {
      await context.close();
    } catch (error) {
      failure = error;
    }
    expect(order).toEqual(["second", "first"]);
    expect(failure).toBeInstanceOf(AggregateError);
    expect(failure.errors.map(error => error.cause)).toEqual([secondError, firstError]);
    expect(failure.errors[0].message).toContain("second");
    expect(failure.errors[1].message).toContain("first");
    await context.close();
    expect(order).toEqual(["second", "first"]);
    expect(() => context.addCleanup("late", () => {})).toThrow("已经关闭");
  });

  test.each([false, true])("并发关闭共同等待全部清理，失败也交给每个等待方: %s", async (cleanupFails) => {
    const context = TestRunContext.fromTestFile(
      path.join(process.cwd(), "tests", "unit", "harness", "concurrent-cleanup.test.mjs"),
    );
    const entered = Promise.withResolvers();
    const release = Promise.withResolvers();
    const failure = new Error("受控资源关闭失败");
    const order = [];
    let settled = 0;
    context.addCleanup("earlier", () => order.push("earlier"));
    context.addCleanup("blocked", async () => {
      entered.resolve();
      await release.promise;
      order.push("blocked");
      if (cleanupFails) throw failure;
    });
    const first = context.close();
    await entered.promise;
    const resultsPromise = Promise.allSettled([first, context.close()].map(async request => {
      try {
        return await request;
      } finally {
        settled += 1;
      }
    }));
    try {
      await Promise.resolve();
      await Promise.resolve();
      expect(settled).toBe(0);
      expect(() => context.addCleanup("late", () => {})).toThrow("已经关闭");
      release.resolve();
      const results = await resultsPromise;
      expect(order).toEqual(["blocked", "earlier"]);
      expect(settled).toBe(2);
      if (cleanupFails) {
        expect(results.map(result => result.status)).toEqual(["rejected", "rejected"]);
        expect(results[0].reason).toBeInstanceOf(AggregateError);
        expect(results[1].reason).toBe(results[0].reason);
        expect(results[0].reason.errors[0].cause).toBe(failure);
      } else {
        expect(results.map(result => result.status)).toEqual(["fulfilled", "fulfilled"]);
      }
      await context.close();
      expect(order).toEqual(["blocked", "earlier"]);
    } finally {
      release.resolve();
      await resultsPromise;
    }
  });
});

describe("显式项目根下的相对测试路径", () => {
  test.each([
    ["tests/unit/test-relative.mjs", "unit/test-relative"],
    ["./tests/unit/../unit/nested/test-relative.mjs", "unit/nested/test-relative"],
  ])("以指定项目根解析 %s，与当前cwd分开", (relativeFile, testId) => {
    const projectRoot = path.join(process.cwd(), "out/tests/harness/js/run-context.test/workspace/project with 中文");
    const context = TestRunContext.fromTestFile(relativeFile, projectRoot);
    const expectedOutput = path.join(projectRoot, "out/tests", testId);

    expect(context.projectRoot).toBe(projectRoot);
    expect(context.testFile).toBe(path.resolve(projectRoot, relativeFile));
    expect(context.testId).toBe(testId);
    expect(context.outputRoot).toBe(expectedOutput);
    expect(context.runRoot).toMatch(path.join(expectedOutput, "runs", "run-"));
    expect(context.workspaceRoot).toBe(path.join(expectedOutput, "workspace"));
    expect(context.artifactsDir).toBe(path.join(context.runRoot, "artifacts"));
  });
});

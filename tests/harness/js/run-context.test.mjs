import { describe, expect, test } from "bun:test";
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
    expect(context.artifactsDir).toBe(path.join(context.outputRoot, "artifacts"));
  });

  test("拒绝 tests 目录之外的文件", () => {
    expect(() => TestRunContext.fromTestFile(
      path.join(process.cwd(), "scripts", "outside.mjs"),
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
});

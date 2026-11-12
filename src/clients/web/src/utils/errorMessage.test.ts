import { describe, expect, test } from "bun:test";

import { HttpRequestError } from "../api/http";
import { errorDisplayMessage, errorMessage, isErrorReported, ReportedError } from "./errorMessage";

test("错误报告归属只认显式回执，混合复合错误仍须报告", () => {
  const cause = new Error("原错误");
  const reported = new ReportedError(cause.message, cause);
  expect(reported.cause).toBe(cause);
  expect(isErrorReported(cause)).toBe(false);
  expect(isErrorReported(new Error("ReportedError: 原错误已报告"))).toBe(false);
  expect(isErrorReported(reported)).toBe(true);
  expect(isErrorReported(new AggregateError([reported, cause]))).toBe(false);
  expect(isErrorReported(new AggregateError([reported, new AggregateError([reported])]))).toBe(true);
  expect(isErrorReported(new AggregateError([]))).toBe(false);
});

function httpError(status: number, statusText: string, detail: unknown): HttpRequestError {
  return new HttpRequestError(status, statusText, detail, "/api/v1/workspace/files", null);
}

describe("errorDisplayMessage 按状态码补可执行提示", () => {
  test("403 前置权限提示并保留原始异常文本", () => {
    const text = errorDisplayMessage(
      httpError(403, "Forbidden", "文件树路径无访问权限: /root/secret"),
    );
    expect(text).toContain("没有访问权限");
    expect(text).toContain("请求失败 403 Forbidden");
    expect(text).toContain("文件树路径无访问权限: /root/secret");
  });

  test("404 前置不存在提示并保留原始异常文本", () => {
    const text = errorDisplayMessage(
      httpError(404, "Not Found", "文件树路径不存在: src/gone"),
    );
    expect(text).toContain("目标不存在");
    expect(text).toContain("请求失败 404 Not Found");
    expect(text).toContain("文件树路径不存在: src/gone");
  });

  test("未覆盖的状态码不臆造提示，退化为原始异常文本", () => {
    const error = httpError(507, "Insufficient Storage", "磁盘已满");
    expect(errorDisplayMessage(error)).toBe(errorMessage(error));
    expect(errorDisplayMessage(error)).not.toContain("没有访问权限");
  });

  test("401 提示刷新或核对凭据并保留原始异常文本", () => {
    const text = errorDisplayMessage(
      httpError(401, "Unauthorized", "invalid local token"),
    );
    expect(text).toContain("刷新页面");
    expect(text).toContain("请求失败 401 Unauthorized");
    expect(text).toContain("invalid local token");
  });

  test("非 HttpRequestError 不带状态码，退化为原始异常文本", () => {
    expect(errorDisplayMessage(new Error("剪贴板不可用"))).toBe("剪贴板不可用");
    expect(errorDisplayMessage("裸字符串错误")).toBe("裸字符串错误");
  });
});

test("错误归一表达式只由共享工具实现，不在组件或 hook 内复制", async () => {
  const sourceRoot = Bun.fileURLToPath(new URL("../", import.meta.url));
  const normalizedExpression = "error instanceof Error ? error.message : String(error)";
  const hits: string[] = [];
  for await (const relativePath of new Bun.Glob("**/*.{ts,tsx}").scan({ cwd: sourceRoot })) {
    if (/\.test\.tsx?$/.test(relativePath)) continue;
    const text = await Bun.file(`${sourceRoot}/${relativePath}`).text();
    if (text.includes(normalizedExpression)) hits.push(relativePath);
  }
  expect(hits).toEqual(["utils/errorMessage.ts"]);
});

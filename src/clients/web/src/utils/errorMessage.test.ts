import { expect, test } from "bun:test";

import { HttpRequestError } from "../api/http";
import { errorMessage, isErrorReported, ReportedError } from "./errorMessage";

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

test.each([401, 403, 404, 409, 507])("状态码 %s 不替后端猜测业务原因", (status) => {
  const failure = httpError(status, "Rejected", "上游模型策略拒绝请求");
  expect(errorMessage(failure)).toContain("上游模型策略拒绝请求");
  expect(errorMessage(failure)).toContain(String(status));
  expect(errorMessage(failure)).not.toMatch(/文件系统权限|刷新页面|目标不存在/);
});

test("未知异常保留原因、清除凭据且摘要有界", () => {
  expect(errorMessage(new Error("连接失败 api_key=private-key"))).toBe("连接失败 api_key=[redacted]");
  expect(new TextEncoder().encode(errorMessage(new Error("汉".repeat(20000)))).length).toBeLessThanOrEqual(8192);
});

import { expect, test } from "bun:test";

import { HttpRequestError } from "../api/http";
import { errorDiagnostic, errorMessage, errorSummary, isErrorReported, ReportedError } from "./errorMessage";
import { RealtimeTransportError } from "../../../../shared/realtime.js";

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
  expect(errorDiagnostic(failure)).toContain(String(status));
  expect(errorSummary(failure)).toBe("上游模型策略拒绝请求");
  expect(errorMessage(failure)).not.toMatch(/文件系统权限|刷新页面|目标不存在/);
});

test("未知异常保留原因、清除凭据且摘要有界", () => {
  expect(errorMessage(new Error("连接失败 api_key=private-key"))).toBe("连接失败 api_key=[redacted]");
  expect(new TextEncoder().encode(errorMessage(new Error("汉".repeat(20000)))).length).toBeLessThanOrEqual(8192);
});

test("安全诊断只读取HTTP已知字段，不遍历或序列化原协议data", () => {
  const detail = { code: "owner_denied", owner: "original-owner", reason: "policy_rejected", data: { toJSON() { throw new Error("不能序列化data"); } },
    details: { source_uri: "boxteam://original/resources/config/workspace", issues: [
      { path: ["field.with.dot", 1], path_exact: true, message: "必须是字符串 api_key=private-key" },
    ], privateData: { toJSON() { throw new Error("不能序列化任意details"); } } } };
  const failure = httpError(409, "Conflict", detail);
  expect(failure.detail).toBe(detail);
  expect(errorSummary(failure)).toBe("未提供错误原因");
  const diagnostic = errorDiagnostic(failure);
  expect(diagnostic).toContain("code: owner_denied");
  expect(diagnostic).toContain("owner: original-owner");
  expect(diagnostic).toContain("source_uri: boxteam://original/resources/config/workspace");
  expect(diagnostic).toContain('issue ["field.with.dot",1] (exact): 必须是字符串 api_key=[redacted]');
  expect(diagnostic).not.toContain("不能序列化data");
  expect(diagnostic).not.toContain("不能序列化任意details");
  expect(diagnostic).not.toContain("private-key");
});

test("常驻订阅只把原订阅关联投影到安全诊断，默认摘要不拼路径或请求身份", () => {
  const transport = new RealtimeTransportError(503, '订阅来源不可读', 'request-realtime', {
    subscriptionId: 7, path: '/api/v1/events?token=private-token',
  });
  const failure = new HttpRequestError(503, 'Realtime failed', transport.message, '/api/v1/events', transport.requestId, null, { cause: transport });
  expect(errorSummary(failure)).toBe('订阅来源不可读');
  expect(errorDiagnostic(failure)).toContain('subscription_id: 7');
  expect(errorDiagnostic(failure)).toContain('request-realtime');
  expect(errorDiagnostic(failure)).toContain('/api/v1/events?token=[redacted]');
  expect(errorDiagnostic(failure)).not.toContain('private-token');
  expect(failure.cause).toBe(transport);
});

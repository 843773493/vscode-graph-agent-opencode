import { afterEach, expect, test } from "bun:test";

import { clearSessionGoal, getSessionGoal } from "../../api";
import { invalidateGatewayToken } from "../http";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

/**
 * 按路径应答本机凭据与业务请求，未声明的请求直接报错；复用端口前作废凭据缓存。
 */
function installGatewayBarrierFetch(
  port: number,
  handler: (url: URL, method: string) => Response | undefined,
): void {
  invalidateGatewayToken(port);

  globalThis.fetch = Object.assign(
    async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(
        input instanceof Request ? input.url : String(input),
        `http://127.0.0.1:${port}`,
      );
      const method = String(
        init?.method ?? (input instanceof Request ? input.method : "GET"),
      );
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return Response.json({
          code: 0,
          message: "ok",
          request_id: "req_gateway_token",
          data: { token: "test-token" },
        });
      }
      const response = handler(url, method);
      if (response === undefined) {
        throw new Error(`测试收到未声明请求: ${method} ${url.pathname}`);
      }
      return response;
    },
    { preconnect: originalFetch.preconnect },
  );
}

test("Goal GET 允许权威 data 为 null", async () => {
  installGatewayBarrierFetch(49_101, (url, method) => {
    if (
      method === "GET"
      && url.pathname === "/api/v1/sessions/ses_without_goal/goal"
    ) {
      return Response.json({
        code: 0,
        message: "ok",
        request_id: "req_goal_null",
        data: null,
      });
    }
    return undefined;
  });

  expect(await getSessionGoal(49_101, "ses_without_goal")).toBeNull();
});

test("Goal GET 缺少 data 字段时快速失败", async () => {
  installGatewayBarrierFetch(49_102, (url, method) => {
    if (
      method === "GET"
      && url.pathname === "/api/v1/sessions/ses_invalid_goal/goal"
    ) {
      return Response.json({
        code: 0,
        message: "ok",
        request_id: "req_goal_missing_data",
      });
    }
    return undefined;
  });

  await expect(getSessionGoal(49_102, "ses_invalid_goal")).rejects.toThrow(
    "后端响应缺少 data 字段",
  );
});

test("Goal DELETE 返回权威 main Thread 身份与清除结果", async () => {
  const result = { session_id: "ses_goal", thread_id: "thr_main", cleared: true };
  installGatewayBarrierFetch(49_103, (url, method) =>
    method === "DELETE" && url.pathname === "/api/v1/sessions/ses_goal/goal"
      ? Response.json({ code: 0, data: result, request_id: "req_goal_clear" }) : undefined);
  expect(await clearSessionGoal(49_103, "ses_goal")).toEqual(result);
});

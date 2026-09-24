import { afterEach, expect, test } from "bun:test";

import { getSessionGoal } from "../../api";
import { invalidateGatewayToken, invalidateGatewayUserSession } from "../http";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

/**
 * 安装本文件统一的 fetch 桩：先真实应答 Gateway 本地凭据与用户会话屏障
 * （GET /api/gateway/users/current、POST /api/gateway/users/guest），再把业务
 * 端点交给 handler；handler 不认领的请求一律抛出。
 *
 * 屏障必须真实应答，不能拿业务响应兜底：api/gateway/userAccess.ts 在模块加载时
 * 注册了生产 initializer，同一进程内 /api/v1/ 请求会先等 users/current 完成。
 * 若把 data:null 当万能兜底，屏障自己就会抛「后端响应缺少 data 字段」，本文件
 * 于是变成顺序依赖——与 gatewayApi.test.ts 同跑时红、单跑时绿。
 *
 * 端口在不同测试文件间可能被复用：进程级凭据缓存与「用户会话已就绪」缓存都必须
 * 显式作废，否则会拿着上一个文件缓存的 token 或已完成的屏障直接跳过本文件的桩。
 */
function installGatewayBarrierFetch(
  port: number,
  handler: (url: URL, method: string) => Response | undefined,
): void {
  invalidateGatewayToken(port);
  invalidateGatewayUserSession(port);
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
      if (url.pathname === "/api/gateway/users/current") {
        return Response.json({
          code: 0,
          message: "ok",
          request_id: "req_current_user",
          data: { kind: "guest", user_id: null },
        });
      }
      if (url.pathname === "/api/gateway/users/guest") {
        return Response.json({
          code: 0,
          message: "ok",
          request_id: "req_guest_user",
          data: { kind: "guest", user_id: null },
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

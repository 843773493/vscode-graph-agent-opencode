import { afterEach, expect, test } from "bun:test";

import { getSessionGoal } from "./api";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("Goal GET 允许权威 data 为 null", async () => {
  globalThis.fetch = Object.assign(
    async (input: string | URL | Request) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return Response.json({
          code: 0,
          message: "ok",
          data: { token: "test-token" },
          request_id: "req_gateway_token",
        });
      }
      return Response.json({
        code: 0,
        message: "ok",
        data: null,
        request_id: "req_goal_null",
      });
    },
    { preconnect: originalFetch.preconnect },
  );

  expect(await getSessionGoal(49_101, "ses_without_goal")).toBeNull();
});

test("Goal GET 缺少 data 字段时快速失败", async () => {
  globalThis.fetch = Object.assign(
    async (input: string | URL | Request) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return Response.json({
          code: 0,
          message: "ok",
          data: { token: "test-token" },
          request_id: "req_gateway_token",
        });
      }
      return Response.json({
        code: 0,
        message: "ok",
        request_id: "req_goal_missing_data",
      });
    },
    { preconnect: originalFetch.preconnect },
  );

  await expect(getSessionGoal(49_102, "ses_invalid_goal")).rejects.toThrow(
    "后端响应缺少 data 字段",
  );
});

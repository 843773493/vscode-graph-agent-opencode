import { afterEach, expect, test } from "bun:test";

import { clearSessionGoal, getSessionGoal, getSessionGoalOperation } from "../../api";
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

test("Goal GET 返回带控制版本的空 Goal 快照", async () => {
  let requestedUrl: URL | undefined;
  installGatewayBarrierFetch(49_101, (url, method) => {
    requestedUrl = url;
    if (
      method === "GET"
      && url.pathname === "/api/v1/sessions/ses_without_goal/goal"
    ) {
      return Response.json({
        code: 0,
        message: "ok",
        request_id: "req_goal_null",
        data: { session_id: "ses_without_goal", thread_id: "thr_main", goal: null,
          control_revision: 0, sync_version: 4 },
      });
    }
    return undefined;
  });

  expect(await getSessionGoal(49_101, "ses_without_goal", null, undefined, 4)).toEqual({
    session_id: "ses_without_goal", thread_id: "thr_main", goal: null,
    control_revision: 0, sync_version: 4,
  });
  expect(requestedUrl?.searchParams.get("minimum_sync_version")).toBe("4");
});

test("Goal GET 拒绝低于请求最低版本的快照", async () => {
  installGatewayBarrierFetch(49_104, (url, method) => method === "GET"
    && url.pathname === "/api/v1/sessions/ses_stale/goal"
    ? Response.json({ code: 0, data: { session_id: "ses_stale", thread_id: "thr_main", goal: null,
      control_revision: 1, sync_version: 3 }, request_id: "req_goal_stale" }) : undefined);
  await expect(getSessionGoal(49_104, "ses_stale", null, undefined, 4)).rejects.toThrow("sync version is stale");
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
  const result = { session_id: "ses_goal", thread_id: "thr_main", operation_id: "op_clear",
    status: "applied", control_revision: 5, sync_version: 8 };
  installGatewayBarrierFetch(49_103, (url, method) =>
    method === "DELETE" && url.pathname === "/api/v1/sessions/ses_goal/goal"
      ? Response.json({ code: 0, data: result, request_id: "req_goal_clear" }) : undefined);
  expect(await clearSessionGoal(49_103, "ses_goal", {
    operation_id: "op_clear", expected_control_revision: 4,
  })).toEqual(result);
});

test.each([
  { session_id: "ses_invalid", thread_id: "main", goal: null, control_revision: 1 },
  { session_id: "other", thread_id: "main", goal: null, control_revision: 1, sync_version: 1 },
  { session_id: "ses_invalid", thread_id: "main", goal: { goal_id: "partial" }, control_revision: 1, sync_version: 1 },
  { session_id: "ses_invalid", thread_id: "main", goal: null, control_revision: 1, sync_version: -1 },
])("Goal GET 拒绝损坏快照，不能把无效协议当成空 Goal: %j", async (data) => {
  installGatewayBarrierFetch(49_105, () => Response.json({ code: 0, data, request_id: "req_invalid_snapshot" }));
  await expect(getSessionGoal(49_105, "ses_invalid")).rejects.toThrow();
});

test("Goal 原操作查询拒绝另一操作的回执", async () => {
  installGatewayBarrierFetch(49_106, () => Response.json({ code: 0, request_id: "req_wrong_operation", data: {
    session_id: "ses_goal", thread_id: "main", operation_id: "other-operation", status: "unknown",
    control_revision: 0, sync_version: 0,
  } }));
  await expect(getSessionGoalOperation(49_106, "ses_goal", "original-operation")).rejects.toThrow("操作身份");
});

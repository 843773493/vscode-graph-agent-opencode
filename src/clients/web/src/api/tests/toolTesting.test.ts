import { afterEach, describe, expect, test } from "bun:test";

import { listToolTestRuns } from "../toolTesting";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

/** 安装带凭据应答的 fetch 桩，items 入参决定业务响应形状。 */
function stubToolTestRunsFetch(items: unknown): void {
  globalThis.fetch = Object.assign(
    async (...args: Parameters<typeof fetch>) => {
      const path = new URL(String(args[0]), "http://127.0.0.1").pathname;
      if (path === "/api/gateway/auth/local-credential") {
        return Response.json({
          code: 0,
          message: "ok",
          data: { token: "tool-test-token" },
          request_id: "req_tool_test_token",
        });
      }
      return Response.json({
        code: 0,
        message: "ok",
        data: items === undefined ? {} : { items },
        request_id: "req_tool_test_runs",
      });
    },
    { preconnect: originalFetch.preconnect },
  );
}

describe("工具测试记录列表载荷校验", () => {
  test("合法记录数组正常返回", async () => {
    stubToolTestRunsFetch([{ run_id: "run_1", status: "succeeded" }]);

    const runs = await listToolTestRuns(49_960);

    expect(runs).toHaveLength(1);
    expect(runs[0].run_id).toBe("run_1");
  });

  test("items 为 null / 对象 / 字符串时响亮失败，不把坏载荷透给消费端", async () => {
    for (const items of [null, {}, "x"]) {
      stubToolTestRunsFetch(items);
      const expected = items === null ? "null" : items === "x" ? "string" : "object";
      await expect(listToolTestRuns(49_961)).rejects.toThrow(
        `工具测试记录列表响应 items 必须是数组，实际为 ${expected}`,
      );
    }
  });

  test("缺失 items 字段同样响亮失败", async () => {
    stubToolTestRunsFetch(undefined);

    await expect(listToolTestRuns(49_962)).rejects.toThrow(
      "工具测试记录列表响应 items 必须是数组，实际为 undefined",
    );
  });
});


import { afterEach, describe, expect, test } from "bun:test";
import {
  getSessionTurnBootstrap,
  getSessionTurnDetails,
  listSessionTurns,
  StaleTurnCursorHttpError,
} from "./sessionTurnHistory";
import { HttpRequestError } from "./http";

const originalFetch = globalThis.fetch;

function apiResponse(data: unknown, status = 200): Response {
  return Response.json(
    { code: status === 200 ? 0 : status, message: "ok", request_id: "req", data },
    { status },
  );
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("Turn 历史 API client", () => {
  test("按约定路径请求 bootstrap、分页和批量详情", async () => {
    const requests: Array<{ path: string; method: string; body: unknown }> = [];
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      const [input, init] = args;
      const url = new URL(String(input));
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "turn-api-token" });
      }
      requests.push({
        path: `${url.pathname}${url.search}`,
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });
      if (url.pathname.endsWith("/bootstrap")) {
        return apiResponse({
          session: {
            session_id: "ses_api",
            workspace_id: "workspace",
            title: "API 测试",
            current_agent_id: "default",
            created_at: "2026-07-28T00:00:00Z",
            updated_at: "2026-07-28T00:00:00Z",
          },
          latest_turn: null,
          active_jobs: [],
          older_cursor: null,
          event_cursor: null,
          projection_epoch: 1,
        });
      }
      return apiResponse({ items: [], projection_epoch: 1 });
    }, originalFetch);

    await getSessionTurnBootstrap(49_211, "ses_api", "workspace");
    await listSessionTurns(49_211, "ses_api", "workspace", {
      cursor: "opaque cursor",
    });
    await getSessionTurnDetails(
      49_211,
      "ses_api",
      ["job_1", "job_2"],
      "workspace",
    );

    expect(requests).toEqual([
      {
        path: "/api/v1/sessions/ses_api/bootstrap",
        method: "GET",
        body: null,
      },
      {
        path: "/api/v1/sessions/ses_api/turns?limit=20&cursor=opaque+cursor",
        method: "GET",
        body: null,
      },
      {
        path: "/api/v1/sessions/ses_api/turns/details",
        method: "POST",
        body: { turn_ids: ["job_1", "job_2"] },
      },
    ]);
  });

  test("把 409 stale cursor 映射为可识别错误", async () => {
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      const [input] = args;
      const url = new URL(String(input));
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "stale-token" });
      }
      return Response.json({
        detail: {
          code: "stale_turn_cursor",
          session_id: "ses_stale",
          cursor_epoch: 1,
          current_epoch: 2,
          message: "历史已重排",
        },
      }, { status: 409, statusText: "Conflict" });
    }, originalFetch);

    expect(
      listSessionTurns(49_212, "ses_stale", "workspace", { cursor: "old" }),
    ).rejects.toBeInstanceOf(StaleTurnCursorHttpError);
  });

  test("结构化后端错误保留可诊断 message", async () => {
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      const [input] = args;
      const url = new URL(String(input));
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "broken-token" });
      }
      return Response.json({
        detail: {
          code: "turn_projection_corrupt",
          message: "Turn manifest 与 index epoch 不一致",
        },
      }, { status: 500, statusText: "Internal Server Error" });
    }, originalFetch);

    try {
      await getSessionTurnBootstrap(49_213, "ses_broken", "workspace");
      throw new Error("预期 bootstrap 请求失败");
    } catch (error) {
      expect(error).toBeInstanceOf(HttpRequestError);
      expect((error as Error).message).toContain(
        "Turn manifest 与 index epoch 不一致",
      );
    }
  });
});

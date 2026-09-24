import { afterEach, describe, expect, test } from "bun:test";
import {
  getSessionTurnBootstrap,
  loadSessionHistory,
  StaleTurnCursorHttpError,
} from "./sessionTurnHistory";
import { HttpRequestError } from "../http";
import {
  installSessionCatalogFetchMock,
  unwrapSessionCatalogFetch,
} from "./sessionApiFetchMock";

function apiResponse(data: unknown, status = 200): Response {
  return Response.json(
    { code: status === 200 ? 0 : status, message: "ok", request_id: "req", data },
    { status },
  );
}

afterEach(() => {
  unwrapSessionCatalogFetch();
});

describe("Turn 历史 API client", () => {
  test("按约定路径请求 bootstrap 和语义化历史", async () => {
    const requests: Array<{ path: string; method: string; body: unknown }> = [];
    installSessionCatalogFetchMock(({ url: rawUrl, path, method, init }) => {
      requests.push({
        path: `${path}${new URL(rawUrl).search}`,
        method: method,
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });
      if (path.endsWith("/bootstrap")) {
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
    }, { credentialToken: "turn-api-token" });

    await getSessionTurnBootstrap(49_211, "ses_api", "workspace");
    await loadSessionHistory(
      49_211,
      "ses_api",
      { direction: "before", cursor: "opaque cursor", turns: 4 },
      "workspace",
    );
    await loadSessionHistory(
      49_211,
      "ses_api",
      {
        direction: "around",
        turn_ids: ["job_1", "job_2"],
        turns: 2,
      },
      "workspace",
    );

    expect(requests).toEqual([
      {
        path: "/api/v1/sessions/ses_api/bootstrap",
        method: "GET",
        body: null,
      },
      {
        path: "/api/v1/sessions/ses_api/history",
        method: "POST",
        body: { direction: "before", cursor: "opaque cursor", turns: 4 },
      },
      {
        path: "/api/v1/sessions/ses_api/history",
        method: "POST",
        body: {
          direction: "around",
          turn_ids: ["job_1", "job_2"],
          turns: 2,
        },
      },
    ]);
  });

  test("把 409 stale cursor 映射为可识别错误", async () => {
    const seen: string[] = [];
    installSessionCatalogFetchMock(({ path, method }) => {
      seen.push(`${method} ${path}`);
      return Response.json({
        detail: {
          code: "stale_turn_cursor",
          session_id: "ses_stale",
          cursor_epoch: 1,
          current_epoch: 2,
          message: "历史已重排",
        },
      }, { status: 409, statusText: "Conflict" });
    }, { credentialToken: "stale-token" });

    expect(
      loadSessionHistory(
        49_212,
        "ses_stale",
        { direction: "before", cursor: "old" },
        "workspace",
      ),
    ).rejects.toBeInstanceOf(StaleTurnCursorHttpError);
    // 桩只应答业务请求（屏障隧道由共享桩内部处理）。若这条断言失败，说明 409
    // 来自屏障请求而非 history 端点：用例会退化成空转，必须响亮失败。
    expect(seen).toEqual(["POST /api/v1/sessions/ses_stale/history"]);
  });

  test("结构化后端错误保留可诊断 message", async () => {
    const seen: string[] = [];
    installSessionCatalogFetchMock(({ path, method }) => {
      seen.push(`${method} ${path}`);
      return Response.json({
        detail: {
          code: "turn_projection_corrupt",
          message: "Turn manifest 与 index epoch 不一致",
        },
      }, { status: 500, statusText: "Internal Server Error" });
    }, { credentialToken: "broken-token" });

    try {
      await getSessionTurnBootstrap(49_213, "ses_broken", "workspace");
      throw new Error("预期 bootstrap 请求失败");
    } catch (error) {
      expect(error).toBeInstanceOf(HttpRequestError);
      expect((error as Error).message).toContain(
        "Turn manifest 与 index epoch 不一致",
      );
    }
    // 同款防空转：错误必须来自 bootstrap 端点本身。
    expect(seen).toEqual(["GET /api/v1/sessions/ses_broken/bootstrap"]);
  });
});

import { afterEach, describe, expect, test } from "bun:test";
import {
  getSessionTurnBootstrap,
  loadSessionHistory,
  searchThreadHistoryMessages,
  StaleTurnCursorHttpError,
} from "./sessionTurnHistory";
import { HttpRequestError } from "../http";
import { gatewayWorkspaceKey } from "../workspaceIdentity";
import {
  installSessionApiFetchMock as installSessionApiFetch,
  unwrapSessionApiFetch,
} from "./sessionApiFetchMock";

const OWNER = { gatewayId: "gateway-turn-history", workspaceId: "00000000-0000-4000-8000-000000000063" };
const WORKSPACE_KEY = gatewayWorkspaceKey(OWNER);
const businessRequests: string[] = [];

function installSessionApiFetchMock(...[handler, options]: Parameters<typeof installSessionApiFetch>): void {
  installSessionApiFetch((request) => {
    expect(request.path.startsWith("/api/v1/sessions/")).toBe(true);
    const headers = new Headers(request.init?.headers);
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(OWNER.gatewayId);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(OWNER.workspaceId);
    businessRequests.push(request.path);
    return handler(request);
  }, options);
}

function apiResponse(data: unknown, status = 200): Response {
  return Response.json(
    { code: status === 200 ? 0 : status, message: "ok", request_id: "req", data },
    { status, headers: { "X-Request-ID": "req" } },
  );
}

afterEach(() => {
  unwrapSessionApiFetch();
  const count = businessRequests.length;
  businessRequests.length = 0;
  expect(count).toBeGreaterThan(0);
});

describe("Turn 历史 API client", () => {
  test("正文查询固定精确 Thread，定位沿原 around 请求传递独立引用", async () => {
    const requests: Array<{ path: string; body: unknown; workspace: string | null }> = [];
    const reference = { item_id: "item-1", content_hash: "a".repeat(64), start_codepoint: 2, end_codepoint: 5 };
    installSessionApiFetchMock(({ path, init }) => {
      requests.push({ path, body: JSON.parse(String(init?.body)), workspace: new Headers(init?.headers).get("X-BoxTeam-Workspace-Id") });
      return apiResponse(path.endsWith("/search")
        ? { session_id: "ses_api", thread_id: "thread_child", items: [], cursor: null, complete: true,
          limited_reason: null, scanned_items: 0, scanned_bytes: 0 }
        : { thread_id: "thread_child", items: [], projection_epoch: 1 });
    });
    await searchThreadHistoryMessages(49_219, "ses_api", "thread_child", { query: "😀词", limit: 3 }, WORKSPACE_KEY);
    await loadSessionHistory(49_219, "ses_api", { direction: "around", anchor_turn_id: "turn-1", anchor_message: reference },
      WORKSPACE_KEY, undefined, "thread_child");
    expect(requests).toEqual([
      { path: "/api/v1/sessions/ses_api/threads/thread_child/history/search", body: { query: "😀词", limit: 3 }, workspace: OWNER.workspaceId },
      { path: "/api/v1/sessions/ses_api/threads/thread_child/history", body: { direction: "around", anchor_turn_id: "turn-1", anchor_message: reference }, workspace: OWNER.workspaceId },
    ]);
  });

  test("按约定路径请求 bootstrap 和语义化历史", async () => {
    const requests: Array<{ path: string; method: string; body: unknown }> = [];
    installSessionApiFetchMock(({ url: rawUrl, path, method, init }) => {
      requests.push({
        path: `${path}${new URL(rawUrl).search}`,
        method: method,
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });
      if (path.endsWith("/bootstrap")) {
        return apiResponse({
          session: {
            session_id: "ses_api",
            workspace_id: OWNER.workspaceId,
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

    await getSessionTurnBootstrap(49_211, "ses_api", WORKSPACE_KEY);
    await loadSessionHistory(
      49_211,
      "ses_api",
      { direction: "before", cursor: "opaque cursor", turns: 4 },
      WORKSPACE_KEY,
    );
    await loadSessionHistory(
      49_211,
      "ses_api",
      {
        direction: "around",
        turn_ids: ["job_1", "job_2"],
        turns: 2,
      },
      WORKSPACE_KEY,
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
    installSessionApiFetchMock(({ path, method }) => {
      seen.push(`${method} ${path}`);
      return Response.json({
        detail: {
          code: "stale_turn_cursor",
          session_id: "ses_stale",
          cursor_epoch: 1,
          current_epoch: 2,
          message: "历史已重排",
        },
      }, { status: 409, statusText: "Conflict", headers: { "X-Request-ID": "request-stale" } });
    }, { credentialToken: "stale-token" });

    const failed = loadSessionHistory(
        49_212,
        "ses_stale",
        { direction: "before", cursor: "old" },
        WORKSPACE_KEY,
      );
    await expect(failed).rejects.toBeInstanceOf(StaleTurnCursorHttpError);
    await expect(failed).rejects.toHaveProperty("requestId", "request-stale");
    // 桩只应答业务请求（屏障隧道由共享桩内部处理）。若这条断言失败，说明 409
    // 来自屏障请求而非 history 端点：用例会退化成空转，必须响亮失败。
    expect(seen).toEqual(["POST /api/v1/sessions/ses_stale/history"]);
  });

  test("结构化后端错误保留可诊断 message", async () => {
    const seen: string[] = [];
    installSessionApiFetchMock(({ path, method }) => {
      seen.push(`${method} ${path}`);
      return Response.json({
        detail: {
          code: "turn_projection_corrupt",
          message: "Turn manifest 与 index epoch 不一致",
        },
      }, { status: 500, statusText: "Internal Server Error" });
    }, { credentialToken: "broken-token" });

    try {
      await getSessionTurnBootstrap(49_213, "ses_broken", WORKSPACE_KEY);
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

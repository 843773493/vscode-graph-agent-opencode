import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { HttpRequestError } from "../../api/http";
import { sessionScopeKey } from "../../state/session/sessionScope";
import type { PendingRequestList, Session } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { usePendingRequestActions } from "./usePendingRequestActions";

const PORT = 49_508;
const WORKSPACE_ID = "gw_pending_error";
const SESSION_ID = "ses_pending_error";
const CACHE_KEY = sessionScopeKey(WORKSPACE_ID, SESSION_ID);

const originalFetch = globalThis.fetch;

type Route = (
  path: string,
  method: string,
) => Response;

let route: Route = () => {
  throw new Error("测试未设置路由");
};
const calls: string[] = [];

function apiResponse(data: unknown, status = 200): Response {
  return Response.json({
    code: 0,
    message: status === 200 ? "ok" : "error",
    request_id: "request-pending-error-test",
    data,
  }, { status });
}

function installFetch(): void {
  globalThis.fetch = Object.assign(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(
        typeof input === "string" || input instanceof URL
          ? String(input)
          : input.url,
      ).pathname;
      const method = String(init?.method ?? "GET");
      if (path === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "local-pending-error-token" });
      }
      if (path === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      calls.push(`${method} ${path}`);
      return route(path, method);
    },
    { preconnect: originalFetch.preconnect },
  );
}

function session(): Session {
  return {
    session_id: SESSION_ID,
    workspace_id: "ws_local",
    title: "待处理队列错误语义",
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: null,
    created_at: "2026-09-22T00:00:00Z",
    updated_at: "2026-09-22T00:00:00Z",
  };
}

function state(): AppState {
  return {
    eventQueuesBySession: new Map(),
    pendingConversations: new Map(),
    activeJobIdsBySession: new Map(),
    unreadSessionKeys: new Set(),
    sessionAttachmentSummaries: new Map(),
    sessionsByWorkspace: new Map(),
    sessionGatewayWorkspaceById: new Map(),
    currentSession: session(),
    currentSessionWorkspaceId: WORKSPACE_ID,
    status: "",
  } as unknown as AppState;
}

function snapshot(
  messageId: string,
  content: string,
  version: number,
): PendingRequestList {
  return {
    session_id: SESSION_ID,
    active_job_id: `job_${messageId}`,
    snapshot_version: version,
    requests: [{
      job_id: `job_${messageId}`,
      message_id: messageId,
      session_id: SESSION_ID,
      content,
      delivery_policy: "after_turn",
      enqueue_sequence: 1,
      position: 0,
      status: "queued",
      agent_id: "default",
      message_created_at: "2026-09-22T00:00:00Z",
      created_at: "2026-09-22T00:00:00Z",
      updated_at: "2026-09-22T00:00:00Z",
      snapshot_version: version,
    }],
  };
}

let currentState = state();
let actions: ReturnType<typeof usePendingRequestActions>;
let renderer: ReactTestRenderer | undefined;

function Harness(): null {
  actions = usePendingRequestActions({
    apiPort: PORT,
    currentSession: session(),
    currentSessionGatewayWorkspaceId: WORKSPACE_ID,
    currentSessionCacheKey: CACHE_KEY,
    setState: (update) => {
      currentState = typeof update === "function"
        ? update(currentState)
        : update;
    },
  });
  return null;
}

async function mount(): Promise<void> {
  currentState = state();
  calls.length = 0;
  installFetch();
  await act(async () => {
    renderer = create(<Harness />);
  });
}

afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  globalThis.fetch = originalFetch;
});

describe("待处理请求动作的失败补偿", () => {
  test("变更失败且补偿重取成功时抛出原始错误", async () => {
    await mount();
    route = (path, method) => {
      if (method === "PATCH") {
        return Response.json({ detail: "队列快照已过期" }, { status: 409 });
      }
      if (path === `/api/v1/sessions/${SESSION_ID}/pending-requests`) {
        return apiResponse(snapshot("msg_a", "服务端真值", 3));
      }
      throw new Error(`未预期请求: ${method} ${path}`);
    };

    const error = await actions.updatePendingRequest("msg_a", "本地修改")
      .then(() => null, (thrown: unknown) => thrown);

    expect(error).toBeInstanceOf(HttpRequestError);
    expect((error as HttpRequestError).status).toBe(409);
    expect((error as Error).message).toContain("队列快照已过期");
    expect((error as Error).message).not.toContain("也失败");
    // 补偿成功时必须用后端真值整表替换本地快照。
    expect(calls).toEqual([
      `PATCH /api/v1/sessions/${SESSION_ID}/pending-requests/msg_a`,
      `GET /api/v1/sessions/${SESSION_ID}/pending-requests`,
    ]);
    expect(currentState.pendingConversations.get(CACHE_KEY)?.[0].conversationId)
      .toBe("msg_a");
    expect(currentState.activeJobIdsBySession.get(CACHE_KEY)).toBe("job_msg_a");
  });

  test("变更失败且补偿重取也失败时仍抛原始错误并拼接补偿信息", async () => {
    await mount();
    route = (path, method) => {
      if (method === "PATCH") {
        return Response.json({ detail: "队列快照已过期" }, { status: 409 });
      }
      if (path === `/api/v1/sessions/${SESSION_ID}/pending-requests`) {
        return Response.json(
          { detail: "待处理队列服务不可用" },
          { status: 500 },
        );
      }
      throw new Error(`未预期请求: ${method} ${path}`);
    };

    const error = await actions.updatePendingRequest("msg_b", "本地修改")
      .then(() => null, (thrown: unknown) => thrown);

    // 必须仍是原始 HttpRequestError 对象：上层按 status 判断语义，不能被包装。
    expect(error).toBeInstanceOf(HttpRequestError);
    expect((error as HttpRequestError).status).toBe(409);
    expect((error as Error).name).toBe("HttpRequestError");
    expect((error as Error).message).toContain("队列快照已过期");
    expect((error as Error).message)
      .toContain("重新读取待处理队列也失败: 请求失败 500");
  });

  test("变更成功后用后端返回的完整对象整表替换快照", async () => {
    await mount();
    route = (_path, method) => {
      if (method === "PATCH") {
        return apiResponse(snapshot("msg_c", "服务端返回内容", 5));
      }
      throw new Error(`未预期请求: ${method}`);
    };

    await actions.updatePendingRequest("msg_c", "本地修改");

    expect(calls).toEqual([
      `PATCH /api/v1/sessions/${SESSION_ID}/pending-requests/msg_c`,
    ]);
    const conversations = currentState.pendingConversations.get(CACHE_KEY);
    expect(conversations).toHaveLength(1);
    expect(conversations?.[0].conversationId).toBe("msg_c");
    expect(conversations?.[0].userMessage?.content).toBe("服务端返回内容");
    expect(conversations?.[0].queueSnapshotVersion).toBe(5);
    expect(currentState.activeJobIdsBySession.get(CACHE_KEY)).toBe("job_msg_c");
  });
});

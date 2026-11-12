import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { afterEach, expect, test } from "bun:test";
import type { Session } from "../../types/backend";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { createSessionHookState } from "../../tests/fixtures/session/appState";
import { refreshWorkspaceSessionList } from "./sessionRefresh";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

const sessions = Array.from({ length: 21 }, (_, index): Session => ({
  session_id: `ses_page_${index}`, workspace_id: "backend-workspace",
  resource_identity: `fixture-${index}`, uri: `boxteam://fixture/${index}`,
  scope: "workspace", title: `第 ${index + 1} 条会话`, title_source: "user",
  current_agent_id: "default", parent_session_id: null,
  created_at: "2026-10-19T00:00:00Z", updated_at: "2026-10-19T00:00:00Z",
}));

for (const hasMore of [true, false]) {
  test(`${hasMore ? "首页外仍存活的" : "完整列表确认移除的"}当前会话按分页事实收敛`, async () => {
    const workspaceId = gatewayWorkspaceKey({ gateway_id: "gateway-test", workspace_id: "backend-workspace" });
    const selected = sessions[20]!;
    const selectedKey = sessionScopeKey(workspaceId, selected.session_id);
    const first = { ...sessions[0]!, title: "后端返回的新标题", updated_at: "2026-10-19T01:00:00Z" };
    const page = [first, ...sessions.slice(1, 20)];
    const urls: string[] = [];
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      urls.push(url);
      const path = new URL(url, "http://workspace").pathname;
      const data = path === "/api/gateway/auth/local-credential" ? { token: "page-test-token" }
          : path === "/api/v1/sessions" ? { items: page, next_cursor: hasMore ? "page-2" : null, has_more: hasMore }
            : undefined;
      if (data === undefined) throw new Error(`未声明请求: ${url}`);
      return Response.json({ code: 0, message: "ok", data, request_id: "page-request" });
    }, { preconnect: originalFetch.preconnect });
    let state = createSessionHookState({ workspaceId, currentSession: selected, sessions });
    state.sessionAttachmentSummaries.set(selectedKey, { count: 1, names: ["附件.txt"], latestAt: null });
    await refreshWorkspaceSessionList(49_751, { gateway_id: "gateway-test", workspace_id: "backend-workspace" }, (update) => {
      state = typeof update === "function" ? update(state) : update;
    });
    expect(state.sessions[0]).toEqual(first);
    expect(state.currentSession?.session_id).toBe(hasMore ? selected.session_id : first.session_id);
    expect(state.sessions.some((value) => value.session_id === selected.session_id)).toBe(hasMore);
    expect(state.sessionAttachmentSummaries.has(selectedKey)).toBe(hasMore);
    expect(urls.filter((url) => new URL(url, "http://workspace").pathname === "/api/v1/sessions")).toHaveLength(1);
    expect(urls.some((url) => url.includes("cursor="))).toBe(false);
  });
}

for (const currentWorkspace of ["active", "other", "implicit"] as const) {
  test(`完整列表刷新按会话owner收敛：${currentWorkspace}`, async () => {
    const workspaceId = gatewayWorkspaceKey({ gateway_id: "gateway-test", workspace_id: "backend-workspace" });
    const otherWorkspaceId = gatewayWorkspaceKey({ gateway_id: "gateway-test", workspace_id: "backend-other-workspace" });
    const original = sessions[20]!;
    const replacement = sessions[0]!;
    const otherSession: Session = {
      ...original,
      session_id: "ses_other_owner",
      workspace_id: "backend-other-workspace",
      title: "另一工作区的当前会话",
    };
    let respond: ((response: Response) => void) | undefined;
    let notifyRequestStarted: () => void = () => {};
    const requestStarted = new Promise<void>((resolve) => { notifyRequestStarted = resolve; });
    const requests: string[] = [];
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const path = new URL(url, "http://workspace").pathname;
      if (path === "/api/v1/sessions") {
        requests.push(url);
        return await new Promise<Response>((resolve) => {
          respond = resolve;
          notifyRequestStarted();
        });
      }
      const data = path === "/api/gateway/auth/local-credential" ? { token: "owner-test-token" }
          : undefined;
      if (data === undefined) throw new Error(`未声明请求: ${url}`);
      return Response.json({ code: 0, message: "ok", data, request_id: "owner-request" });
    }, { preconnect: originalFetch.preconnect });
    let state = createSessionHookState({
      workspaceId, currentSession: original, sessions: [original],
      sessionsByWorkspace: new Map([[workspaceId, [original]], [otherWorkspaceId, [otherSession]]]),
    });
    const otherKey = sessionScopeKey(otherWorkspaceId, otherSession.session_id);
    const otherAttachments = { count: 1, names: ["另一工作区.txt"], latestAt: null };
    state.sessionAttachmentSummaries.set(otherKey, otherAttachments);
    const refresh = refreshWorkspaceSessionList(49_752, { gateway_id: "gateway-test", workspace_id: "backend-workspace" }, (update) => { state = typeof update === "function" ? update(state) : update; });
    // 请求在途时切换查看对象，回执只能收敛其精确工作区的会话。
    await requestStarted;
    expect(respond).toBeDefined();
    if (currentWorkspace === "other") {
      state = { ...state, currentSession: otherSession, currentSessionWorkspaceId: otherWorkspaceId };
    } else if (currentWorkspace === "active") {
      state = { ...state, activeGatewayWorkspaceId: otherWorkspaceId, sessions: [otherSession] };
    } else {
      state = { ...state, currentSessionWorkspaceId: null };
    }
    respond!(Response.json({ code: 0, message: "ok", request_id: "owner-request",
      data: { items: [replacement], next_cursor: null, has_more: false } }));
    await refresh;
    expect(requests).toHaveLength(1);
    expect(state.currentSession).toEqual(currentWorkspace === "other" ? otherSession : replacement);
    expect(state.currentSessionWorkspaceId).toBe(currentWorkspace === "other" ? otherWorkspaceId : workspaceId);
    expect(state.sessionsByWorkspace.get(workspaceId)).toEqual([replacement]);
    expect(state.sessionsByWorkspace.get(otherWorkspaceId)).toEqual([otherSession]);
    expect(state.sessions).toEqual(currentWorkspace === "active" ? [otherSession] : [replacement]);
    expect(state.activeGatewayWorkspaceId).toBe(currentWorkspace === "active" ? otherWorkspaceId : workspaceId);
    expect(state.sessionAttachmentSummaries.get(otherKey)).toEqual(otherAttachments);
  });
}

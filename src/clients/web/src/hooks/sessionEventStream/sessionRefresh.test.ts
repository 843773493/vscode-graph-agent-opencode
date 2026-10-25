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
    const workspaceId = `gw_page_${hasMore}`;
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
        : path === "/api/gateway/users/current" ? { kind: "guest", user_id: null }
          : path === "/api/v1/sessions" ? { items: page, next_cursor: hasMore ? "page-2" : null, has_more: hasMore }
            : undefined;
      if (data === undefined) throw new Error(`未声明请求: ${url}`);
      return Response.json({ code: 0, message: "ok", data, request_id: "page-request" });
    }, { preconnect: originalFetch.preconnect });
    let state = createSessionHookState({ workspaceId, currentSession: selected, sessions });
    state.sessionAttachmentSummaries.set(selectedKey, { count: 1, names: ["附件.txt"], latestAt: null });
    await refreshWorkspaceSessionList(49_751, { workspace_id: workspaceId,
      backend_workspace_id: "backend-workspace", connection_kind: "local" }, (update) => {
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

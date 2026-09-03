import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { AppState } from "../types/frontend";
import type { Session } from "../types/backend";
import { useSessionChangesLoader } from "./useSessionChangesLoader";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function apiResponse(data: unknown): Response {
  return new Response(JSON.stringify({
    code: 0,
    message: "ok",
    data,
    request_id: "request-test",
  }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function session(): Session {
  return {
    session_id: "ses_changes_loader",
    workspace_id: "ws_changes_loader",
    title: "变更加载器测试",
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: null,
    created_at: "2026-09-02T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
  };
}

function state(currentSession: Session): AppState {
  return {
    currentSession,
    contentView: "changes",
    sessionChangesLoading: false,
    sessionChangesError: null,
    sessionChangesets: [],
    selectedChangesetId: null,
    activeChangeset: null,
  } as unknown as AppState;
}

describe("会话文件变更请求协调", () => {
  test("并发和已缓存的变更列表只读取一次，显式刷新才重新读取列表", async () => {
    const currentSession = session();
    let currentState = state(currentSession);
    let loadSessionChangesets:
      | ReturnType<typeof useSessionChangesLoader>["loadSessionChangesets"];
    let refreshSessionChanges:
      | ReturnType<typeof useSessionChangesLoader>["refreshSessionChanges"];
    let invalidateSessionChanges:
      | ReturnType<typeof useSessionChangesLoader>["invalidateSessionChanges"];
    let changesetListRequestCount = 0;
    let changesetDetailRequestCount = 0;

    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const parsed = new URL(url);
      if (parsed.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "local-test-token" });
      }
      if (parsed.pathname === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (
        /^\/api\/v1\/sessions\/[^/]+\/changesets$/.test(parsed.pathname)
      ) {
        changesetListRequestCount += 1;
        const requestedSessionId = parsed.pathname.split("/")[4];
        return apiResponse({
          items: [{
            changeset_id: "cs_default",
            session_id: requestedSessionId,
            title: "默认变更",
            is_default: true,
            summary: { files: 1, additions: 2, deletions: 0 },
          }],
        });
      }
      if (
        /^\/api\/v1\/sessions\/[^/]+\/changesets\/cs_default$/.test(parsed.pathname)
      ) {
        changesetDetailRequestCount += 1;
        const requestedSessionId = parsed.pathname.split("/")[4];
        return apiResponse({
          changeset_id: "cs_default",
          session_id: requestedSessionId,
          title: "默认变更",
          status: "ready",
          summary: { files: 1, additions: 2, deletions: 0 },
          files: [],
        });
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    function Harness(): React.ReactNode {
      const loader = useSessionChangesLoader({
        apiPort: 49_403,
        currentSession,
        workspaceId: "ws_changes_loader",
        setState: (update) => {
          currentState = typeof update === "function"
            ? update(currentState)
            : update;
        },
      });
      loadSessionChangesets = loader.loadSessionChangesets;
      invalidateSessionChanges = loader.invalidateSessionChanges;
      refreshSessionChanges = loader.refreshSessionChanges;
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });

    const first = loadSessionChangesets!(currentSession.session_id);
    const second = loadSessionChangesets!(currentSession.session_id);
    await Promise.all([first, second]);
    await refreshSessionChanges!(currentSession.session_id, "cs_default");
    await refreshSessionChanges!(
      currentSession.session_id,
      "cs_default",
      { refreshList: true },
    );
    await loadSessionChangesets!("ses_other_changes_loader");
    invalidateSessionChanges!();
    await loadSessionChangesets!(currentSession.session_id);

    expect(changesetListRequestCount).toBe(3);
    expect(changesetDetailRequestCount).toBe(2);
    expect(currentState.activeChangeset?.changeset_id).toBe("cs_default");
    act(() => renderer!.unmount());
  });
});

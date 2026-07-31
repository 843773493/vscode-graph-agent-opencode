import React from "react";
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import type { AppState } from "../types/frontend";
import type { Session } from "../types/backend";
import { sessionScopeKey } from "../state/session/sessionScope";
import { useSessionLifecycleActions } from "./useSessionLifecycleActions";

const WORKSPACE_ID = "gw_read_state";
const SESSION_ID = "ses_read_state";
const CACHE_KEY = sessionScopeKey(WORKSPACE_ID, SESSION_ID);

function session(): Session {
  return {
    session_id: SESSION_ID,
    workspace_id: "ws_local",
    title: "未读状态测试",
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: null,
    created_at: "2026-07-24T00:00:00Z",
    updated_at: "2026-07-24T00:00:00Z",
  };
}

function state(value: Session): AppState {
  return {
    sessions: [value],
    sessionsByWorkspace: new Map([[WORKSPACE_ID, [value]]]),
    sessionGatewayWorkspaceById: new Map([[CACHE_KEY, WORKSPACE_ID]]),
    sessionAttachmentSummaries: new Map(),
    eventQueuesBySession: new Map(),
    pendingConversations: new Map(),
    activeJobIdsBySession: new Map(),
    unreadSessionKeys: new Set([CACHE_KEY]),
    activeGatewayWorkspaceId: WORKSPACE_ID,
    currentSession: value,
    currentSessionWorkspaceId: WORKSPACE_ID,
    contentView: "default",
    sessionHistoryReloadNonce: 0,
    status: "",
  } as AppState;
}

describe("会话已读状态", () => {
  test("用户打开会话时清除未读蓝标", () => {
    const currentSession = session();
    let currentState = state(currentSession);
    let selectSession: ((sessionId: string) => void) | undefined;

    function Harness() {
      selectSession = useSessionLifecycleActions({
        apiPort: 8014,
        currentSession,
        activeGatewayWorkspaceId: WORKSPACE_ID,
        currentSessionGatewayWorkspaceId: WORKSPACE_ID,
        currentSessionCacheKey: CACHE_KEY,
        defaultGatewayWorkspaceId: WORKSPACE_ID,
        setState: (update) => {
          currentState = typeof update === "function"
            ? update(currentState)
            : update;
        },
        abortCurrentStream: () => undefined,
        invalidateAgentState: () => undefined,
      }).selectSession;
      return null;
    }

    renderToStaticMarkup(<Harness />);
    selectSession?.(SESSION_ID);

    expect(currentState.unreadSessionKeys.has(CACHE_KEY)).toBe(false);
  });
});

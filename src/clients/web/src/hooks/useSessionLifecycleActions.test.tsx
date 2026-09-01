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

function session(
  sessionId: string = SESSION_ID,
  title: string = "未读状态测试",
): Session {
  return {
    session_id: sessionId,
    workspace_id: "ws_local",
    title,
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: null,
    created_at: "2026-07-24T00:00:00Z",
    updated_at: "2026-07-24T00:00:00Z",
  };
}

function state(value: Session): AppState {
  return {
    gatewayWorkspaces: [],
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
  } as unknown as AppState;
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

  test("重复打开当前会话不会重启历史加载", () => {
    const currentSession = session();
    let currentState = state(currentSession);
    let abortCount = 0;
    let selectWorkspaceSession:
      ((workspaceId: string, sessionId: string, sessionOverride?: Session) => void)
      | undefined;

    function Harness() {
      selectWorkspaceSession = useSessionLifecycleActions({
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
        abortCurrentStream: () => {
          abortCount += 1;
        },
        invalidateAgentState: () => undefined,
      }).selectWorkspaceSession;
      return null;
    }

    renderToStaticMarkup(<Harness />);
    selectWorkspaceSession?.(WORKSPACE_ID, SESSION_ID, currentSession);

    expect(abortCount).toBe(0);
    expect(currentState.sessionHistoryReloadNonce).toBe(0);
    expect(currentState.unreadSessionKeys.has(CACHE_KEY)).toBe(false);
  });

  test("可以用目录节点返回的会话摘要立即打开尚未加载到列表的会话", () => {
    const currentSession = session();
    const targetSession = session("ses_catalog_only", "目录中的会话");
    let currentState = state(currentSession);
    let selectWorkspaceSession:
      ((workspaceId: string, sessionId: string, sessionOverride?: Session) => void)
      | undefined;

    function Harness() {
      selectWorkspaceSession = useSessionLifecycleActions({
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
      }).selectWorkspaceSession;
      return null;
    }

    renderToStaticMarkup(<Harness />);
    selectWorkspaceSession?.(WORKSPACE_ID, targetSession.session_id, targetSession);

    expect(currentState.currentSession).toEqual(targetSession);
    expect(
      currentState.sessionsByWorkspace.get(WORKSPACE_ID)?.[0],
    ).toEqual(targetSession);
    expect(currentState.sessionHistoryReloadNonce).toBe(0);
  });
});
